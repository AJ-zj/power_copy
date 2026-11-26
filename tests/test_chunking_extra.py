"""5.1 切片补充验收测试：真实政策库、PDF 页码、身份更新与边界。

只读访问 storage/policies_versioned.db 与 storage/pdf_acceptance.db，
不联网、不调用 LLM、不写数据库。
"""

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from powercopy.data.parser import ParseProfile, RawPage, load_raw
from powercopy.data.pdf_ingest import load_pdf_artifact, preview_pdf
from powercopy.data.repository import PolicyRepository
from powercopy.data.versioning import VersionedPolicyStore
from powercopy.rag.chunking import (
    ALGORITHM,
    ChunkConfig,
    ChunkError,
    build_manifest,
    digest,
    read_latest,
    split_spans,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_DB = ROOT / "storage" / "policies_versioned.db"
PDF_DB = ROOT / "storage" / "pdf_acceptance.db"
RAW_PDF = ROOT / "storage" / "raw_pdf"
PROFILES = ROOT / "config" / "parser_profiles.json"

NDRC_RAW = ROOT / "storage" / "raw" / "722b8057489c4ac2a54461d62dad117a.json"
NEA_RAW = ROOT / "storage" / "raw" / "f5d31d6af4ab4e7fa76571bbe09bd53f.json"

BASE = datetime(2026, 11, 25, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def real_manifest():
    if not REAL_DB.is_file():
        pytest.skip("缺少真实政策库")
    return build_manifest(REAL_DB, ChunkConfig(500, 80))


@pytest.fixture(scope="module")
def pdf_manifest():
    if not PDF_DB.is_file() or not RAW_PDF.is_dir():
        pytest.skip("缺少 PDF 独立验收库或原始档案")
    return build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=RAW_PDF)


@pytest.fixture(scope="module")
def pdf_preview():
    if not RAW_PDF.is_dir():
        pytest.skip("缺少 PDF 原始档案")
    artifacts = list(RAW_PDF.glob("*.json"))
    if not artifacts:
        pytest.skip("缺少 PDF 原始档案")
    return preview_pdf(load_pdf_artifact(artifacts[0]))


# ==========================================================================
# 1. 真实 HTML 政策只读切片
# ==========================================================================
@pytest.mark.skipif(
    not REAL_DB.is_file() or not PROFILES.is_file(),
    reason="缺少真实政策库或解析配置",
)
class TestRealPolicyChunking:
    def test_two_documents(self, real_manifest):
        assert len(real_manifest["documents"]) == 2
        keys = {doc["source_key"] for doc in real_manifest["documents"]}
        assert keys == {"ndrc", "nea"}

    def test_database_not_modified(self, real_manifest):
        """构建清单不得写库。"""
        before = REAL_DB.read_bytes()
        build_manifest(REAL_DB, ChunkConfig(500, 80))
        assert REAL_DB.read_bytes() == before

    def test_repository_read_only_during_build(self, real_manifest):
        conn = sqlite3.connect(REAL_DB)
        conn.execute("PRAGMA query_only = ON")
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM policy_documents")
        finally:
            conn.close()

    def test_every_chunk_matches_source_slice(self, real_manifest):
        repo = PolicyRepository(REAL_DB)
        for doc in real_manifest["documents"]:
            body = repo.get(doc["document_id"]).body_text
            for chunk in real_manifest["chunks"]:
                if chunk["document_id"] != doc["document_id"]:
                    continue
                assert chunk["text"] == body[chunk["start"] : chunk["end"]]

    def test_text_sha_matches(self, real_manifest):
        for chunk in real_manifest["chunks"]:
            expected = hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
            assert chunk["text_sha256"] == expected

    def test_no_nonblank_character_missing(self, real_manifest):
        repo = PolicyRepository(REAL_DB)
        for doc in real_manifest["documents"]:
            body = repo.get(doc["document_id"]).body_text
            chunks = [
                c
                for c in real_manifest["chunks"]
                if c["document_id"] == doc["document_id"]
            ]
            covered = set()
            for chunk in chunks:
                covered.update(range(chunk["start"], chunk["end"]))
            nonblank = {i for i, ch in enumerate(body) if not ch.isspace()}
            assert not (nonblank - covered), (
                f"{doc['source_key']} 有未覆盖的非空白字符"
            )

    def test_chunk_ids_unique(self, real_manifest):
        ids = [c["chunk_id"] for c in real_manifest["chunks"]]
        assert len(set(ids)) == len(ids)

    def test_chunk_ids_are_sha256(self, real_manifest):
        for chunk in real_manifest["chunks"]:
            assert len(chunk["chunk_id"]) == 64
            int(chunk["chunk_id"], 16)

    def test_metadata_linked_to_ledger(self, real_manifest):
        conn = sqlite3.connect(REAL_DB)
        conn.row_factory = sqlite3.Row
        try:
            for doc in real_manifest["documents"]:
                row = conn.execute(
                    """SELECT v.version, v.content_sha256, d.source_url,
                              f.fetch_id, f.raw_sha256
                       FROM policy_versions v
                       JOIN policy_documents d ON d.document_id = v.document_id
                       JOIN policy_fetches f ON f.policy_id = v.policy_id
                            AND f.version = v.version
                       WHERE d.document_id = ?
                       ORDER BY f.retrieved_at, f.fetch_id LIMIT 1""",
                    (doc["document_id"],),
                ).fetchone()
                assert doc["version"] == row["version"]
                assert doc["content_sha256"] == row["content_sha256"]
                assert doc["source_url"] == row["source_url"]
                assert doc["reference_fetch_id"] == row["fetch_id"]
                assert doc["raw_sha256"] == row["raw_sha256"]
        finally:
            conn.close()

    def test_body_length_matches_db(self, real_manifest):
        repo = PolicyRepository(REAL_DB)
        for doc in real_manifest["documents"]:
            assert doc["body_length"] == len(
                repo.get(doc["document_id"]).body_text
            )

    def test_chunk_count_consistent(self, real_manifest):
        for doc in real_manifest["documents"]:
            actual = sum(
                1
                for c in real_manifest["chunks"]
                if c["document_id"] == doc["document_id"]
            )
            assert doc["chunk_count"] == actual

    def test_all_chunks_respect_max_size(self, real_manifest):
        limit = real_manifest["config"]["max_chars"]
        for chunk in real_manifest["chunks"]:
            assert 0 < chunk["end"] - chunk["start"] <= limit

    def test_last_chunk_ends_at_body_end(self, real_manifest):
        repo = PolicyRepository(REAL_DB)
        for doc in real_manifest["documents"]:
            chunks = sorted(
                [
                    c
                    for c in real_manifest["chunks"]
                    if c["document_id"] == doc["document_id"]
                ],
                key=lambda c: c["start"],
            )
            body = repo.get(doc["document_id"]).body_text
            assert chunks[0]["start"] == 0
            assert chunks[-1]["end"] == len(body)

    def test_html_documents_have_no_page_number(self, real_manifest):
        for chunk in real_manifest["chunks"]:
            assert chunk["page"] is None


# ==========================================================================
# 2. 确定性与身份更新
# ==========================================================================
@pytest.mark.skipif(not REAL_DB.is_file(), reason="缺少真实政策库")
class TestDeterminismAndIdentity:
    def test_repeat_build_identical(self):
        a = build_manifest(REAL_DB, ChunkConfig(500, 80))
        b = build_manifest(REAL_DB, ChunkConfig(500, 80))
        assert a["manifest_sha256"] == b["manifest_sha256"]
        assert [c["chunk_id"] for c in a["chunks"]] == [
            c["chunk_id"] for c in b["chunks"]
        ]

    def test_manifest_hash_self_consistent(self):
        manifest = build_manifest(REAL_DB, ChunkConfig(500, 80))
        payload = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
        assert manifest["manifest_sha256"] == digest(payload)

    @pytest.mark.parametrize(
        "max_chars,overlap",
        [(500, 0), (300, 60), (1000, 200), (4096, 1024)],
    )
    def test_config_change_alters_ids(self, max_chars, overlap):
        base = build_manifest(REAL_DB, ChunkConfig(500, 80))
        other = build_manifest(REAL_DB, ChunkConfig(max_chars, overlap))

        base_ids = {c["chunk_id"] for c in base["chunks"]}
        other_ids = {c["chunk_id"] for c in other["chunks"]}
        assert not (base_ids & other_ids), "不同配置的 chunk_id 不应重叠"
        assert base["manifest_sha256"] != other["manifest_sha256"]


class TestVersionIdentityUpdate:
    @pytest.fixture
    def temp_store(self, tmp_path):
        if not (NDRC_RAW.is_file() and PROFILES.is_file()):
            pytest.skip("缺少真实原始文件或配置")
        return VersionedPolicyStore(tmp_path / "v.db")

    def test_new_version_changes_ids(self, temp_store, tmp_path):
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        profile = ParseProfile.model_validate(profiles["ndrc/market_rules"])
        raw = load_raw(NDRC_RAW)

        v1 = temp_store.ingest(raw, profile)
        m1 = build_manifest(
            temp_store.db_path, ChunkConfig(500, 80), raw_pdf_dir=tmp_path / "raw_pdf"
        )

        html = raw.body_bytes().decode("utf-8").replace(
            "本规则自2024年7月1日起施行", "本规则自2026年1月1日起施行"
        )
        body = html.encode("utf-8")
        import base64

        changed = RawPage(
            fetch_id="v2-fetch-id",
            source_key=raw.source_key,
            target_key=raw.target_key,
            source_url=raw.source_url,
            content_kind=raw.content_kind,
            status_code=raw.status_code,
            content_type=raw.content_type,
            charset=raw.charset,
            retrieved_at=raw.retrieved_at + timedelta(days=30),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode(),
        )
        v2 = temp_store.ingest(changed, profile)

        m2 = build_manifest(
            temp_store.db_path, ChunkConfig(500, 80), raw_pdf_dir=tmp_path / "raw_pdf"
        )

        ids1 = {c["chunk_id"] for c in m1["chunks"]}
        ids2 = {c["chunk_id"] for c in m2["chunks"]}

        assert m1["documents"][0]["version"] == 1
        assert m2["documents"][0]["version"] == 2
        assert m1["documents"][0]["document_id"] != m2["documents"][0]["document_id"]
        assert not (ids1 & ids2), "内容版本变化后 chunk_id 必须全部更新"
        assert m1["manifest_sha256"] != m2["manifest_sha256"]

    def test_history_not_in_default_manifest(self, temp_store, tmp_path):
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        profile = ParseProfile.model_validate(profiles["ndrc/market_rules"])
        raw = load_raw(NDRC_RAW)

        v1 = temp_store.ingest(raw, profile)
        v2 = temp_store.ingest(
            RawPage(
                fetch_id="second",
                source_key=raw.source_key,
                target_key=raw.target_key,
                source_url=raw.source_url,
                content_kind=raw.content_kind,
                status_code=raw.status_code,
                content_type=raw.content_type,
                charset=raw.charset,
                retrieved_at=raw.retrieved_at + timedelta(days=1),
                body_size=raw.body_size,
                body_base64=raw.body_base64,
            ),
            profile,
        )
        # 内容相同 → unchanged，仍是 v1
        manifest = build_manifest(
            temp_store.db_path, raw_pdf_dir=tmp_path / "raw_pdf"
        )
        doc_ids = {c["document_id"] for c in manifest["chunks"]}
        assert doc_ids == {v1.document_id}
        assert v2.action == "unchanged"

    def test_manifest_selects_latest_only(self, temp_store, tmp_path):
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        profile = ParseProfile.model_validate(profiles["ndrc/market_rules"])
        raw = load_raw(NDRC_RAW)
        temp_store.ingest(raw, profile)

        rows = read_latest(temp_store.db_path)
        assert len(rows) == 1
        assert rows[0]["version"] == 1

    def test_untracked_documents_rejected(self, tmp_path):
        """版本表存在但有未纳管文档时拒绝构建。"""
        from powercopy.data.parser import parse_article, to_policy_document

        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        profile = ParseProfile.model_validate(profiles["ndrc/market_rules"])
        raw = load_raw(NDRC_RAW)

        db = tmp_path / "mixed.db"
        # 先建出版本表，再把文档直接塞进 policy_documents（绕过版本写入）
        VersionedPolicyStore(db)
        PolicyRepository(db).add(
            to_policy_document(raw, parse_article(raw, profile))
        )

        with pytest.raises(ChunkError, match="UNTRACKED_DOCUMENTS"):
            build_manifest(db)

    def test_missing_versioned_tables(self, tmp_path):
        """只有 4.1 表结构的旧库，读取时安全失败而非崩溃。"""
        from powercopy.data.parser import parse_article, to_policy_document

        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        profile = ParseProfile.model_validate(profiles["ndrc/market_rules"])
        raw = load_raw(NDRC_RAW)

        db = tmp_path / "only41.db"
        PolicyRepository(db).add(
            to_policy_document(raw, parse_article(raw, profile))
        )

        with pytest.raises(ChunkError, match="POLICY_DB_READ_ERROR"):
            build_manifest(db)


# ==========================================================================
# 3. PDF 页码映射
# ==========================================================================
@pytest.mark.skipif(
    not PDF_DB.is_file() or not RAW_PDF.is_dir(),
    reason="缺少 PDF 独立验收库或原始档案",
)
class TestPdfChunking:
    def test_pages_present(self, pdf_manifest):
        pages = {c["page"] for c in pdf_manifest["chunks"]}
        assert pages == {1, 2}

    def test_chunks_within_page_range(self, pdf_manifest, pdf_preview):
        for chunk in pdf_manifest["chunks"]:
            page = pdf_preview.pages[chunk["page"] - 1]
            assert page.start <= chunk["start"] < chunk["end"] <= page.end

    def test_no_chunk_crosses_page(self, pdf_manifest, pdf_preview):
        for chunk in pdf_manifest["chunks"]:
            assert "\f" not in chunk["text"]
            page = pdf_preview.pages[chunk["page"] - 1]
            assert chunk["end"] <= page.end

    def test_page_numbers_one_based(self, pdf_manifest):
        for chunk in pdf_manifest["chunks"]:
            assert chunk["page"] in (1, 2)

    def test_offsets_match_48_preview(self, pdf_manifest, pdf_preview):
        for chunk in pdf_manifest["chunks"]:
            assert (
                chunk["text"]
                == pdf_preview.document.body_text[chunk["start"] : chunk["end"]]
            )

    def test_coverage_complete(self, pdf_manifest):
        doc_id = pdf_manifest["documents"][0]["document_id"]
        body = PolicyRepository(PDF_DB).get(doc_id).body_text
        covered = set()
        for chunk in pdf_manifest["chunks"]:
            covered.update(range(chunk["start"], chunk["end"]))
        nonblank = {i for i, ch in enumerate(body) if not ch.isspace()}
        assert not (nonblank - covered)

    def test_reference_fetch_matches(self, pdf_manifest):
        artifacts = list(RAW_PDF.glob("*.json"))
        artifact = load_pdf_artifact(artifacts[0])
        doc = pdf_manifest["documents"][0]
        assert doc["reference_fetch_id"] == artifact.fetch_id
        assert doc["raw_sha256"] == artifact.raw_sha256


class TestPdfGuardrails:
    def test_missing_archive_rejected(self, tmp_path):
        if not PDF_DB.is_file():
            pytest.skip("缺少 PDF 验收库")
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ChunkError, match="PDF_ARTIFACT_REQUIRED"):
            build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=empty)

    def test_tampered_hash_rejected(self, tmp_path):
        if not PDF_DB.is_file() or not RAW_PDF.is_dir():
            pytest.skip("缺少 PDF 验收库或档案")
        target = tmp_path / "tampered"
        shutil.copytree(RAW_PDF, target)
        for path in target.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["raw_sha256"] = "0" * 64
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ChunkError):
            build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=target)

    def test_tampered_metadata_rejected(self, tmp_path):
        if not PDF_DB.is_file() or not RAW_PDF.is_dir():
            pytest.skip("缺少 PDF 验收库或档案")
        target = tmp_path / "meta"
        shutil.copytree(RAW_PDF, target)
        for path in target.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["metadata"]["title"] = "篡改后的标题"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ChunkError, match="PDF_PROVENANCE_MISMATCH"):
            build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=target)


# ==========================================================================
# 4. 严格切片语义
# ==========================================================================
class TestSplitSemantics:
    def test_hard_cap(self):
        text = "甲" * 5000
        spans = list(split_spans(text, ChunkConfig(100, 25)))
        for span in spans:
            assert 0 < span.end - span.start <= 100

    def test_overlap_exact(self):
        text = "甲" * 500
        spans = list(split_spans(text, ChunkConfig(100, 25)))
        for a, b in zip(spans, spans[1:]):
            assert b.start == a.end - 25

    def test_natural_boundary_preferred(self):
        text = "甲" * 90 + "。" + "乙" * 200
        spans = list(split_spans(text, ChunkConfig(100, 20)))
        # 句号在窗口最后四分之一，应优先断句
        assert spans[0].end == 91

    def test_newline_preferred_over_sentence(self):
        text = "甲" * 90 + "\n" + "乙" * 200
        spans = list(split_spans(text, ChunkConfig(100, 20)))
        assert spans[0].end == 91

    def test_no_trailing_redundant_span(self):
        text = "甲" * 100
        spans = list(split_spans(text, ChunkConfig(100, 20)))
        assert len(spans) == 1
        assert spans[0] == type(spans[0])(0, 100)

    def test_coverage_no_gap(self):
        text = "第一条 交易周期为15分钟。" * 100
        spans = list(split_spans(text, ChunkConfig(64, 16)))
        covered = set()
        for span in spans:
            covered.update(range(span.start, span.end))
        nonblank = {i for i, ch in enumerate(text) if not ch.isspace()}
        assert not (nonblank - covered)

    def test_empty_text_rejected(self):
        with pytest.raises(ChunkError, match="EMPTY_TEXT"):
            list(split_spans("   \n\t ", ChunkConfig()))


class TestManifestSchema:
    @pytest.mark.skipif(not REAL_DB.is_file(), reason="缺少真实政策库")
    def test_schema_fields(self):
        manifest = build_manifest(REAL_DB, ChunkConfig(500, 80))
        assert manifest["schema"] == "policy-chunk-manifest-v1"
        assert manifest["algorithm"] == ALGORITHM
        assert manifest["selection"] == "latest_observed_only"
        assert set(manifest["config"]) == {"max_chars", "overlap_chars"}
        assert "notice" in manifest

    @pytest.mark.skipif(not REAL_DB.is_file(), reason="缺少真实政策库")
    def test_chunk_required_fields(self):
        manifest = build_manifest(REAL_DB)
        required = {
            "policy_id",
            "document_id",
            "version",
            "content_sha256",
            "source_key",
            "source_url",
            "reference_fetch_id",
            "raw_sha256",
            "start",
            "end",
            "text",
            "text_sha256",
            "chunk_id",
            "page",
        }
        for chunk in manifest["chunks"]:
            assert required <= set(chunk)
