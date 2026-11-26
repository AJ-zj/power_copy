"""4.8 PDF 摄取补充验收测试。

覆盖开发包 23 项之外的两类内容：
1. 重构前旧 HTML 数据库的重放回归（README 明确要求补充，使用备份副本）；
2. 中文模拟 PDF 的逐页/页码映射与业务边界细节。

不联网、不调用 LLM、不写正式政策库。
"""

import base64
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from powercopy.data.parser import ParseProfile, load_raw
from powercopy.data.pdf_ingest import (
    MAX_BYTES,
    PAGE_BREAK,
    PdfArtifact,
    PdfIngestError,
    PdfMetadata,
    _sha,
    import_pdf,
    ingest_pdf,
    load_pdf_artifact,
    preview_pdf,
)
from powercopy.data.query import PolicyQueryService
from powercopy.data.repository import PolicyRepository
from powercopy.data.versioning import VersionedPolicyStore, VersionError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PDF = ROOT / "tests" / "fixtures" / "synthetic_policy_zh.pdf"
PROFILES = ROOT / "config" / "parser_profiles.json"
REAL_HTML_DB = ROOT / "storage" / "policies_versioned.db"

NDRC_RAW = ROOT / "storage" / "raw" / "722b8057489c4ac2a54461d62dad117a.json"
NEA_RAW = ROOT / "storage" / "raw" / "f5d31d6af4ab4e7fa76571bbe09bd53f.json"

BASE = datetime(2026, 11, 25, tzinfo=timezone.utc)


def metadata(**changes):
    data = dict(
        source_key="nea_east",
        target_key="synthetic_fixture",
        source_url="https://hdj.nea.gov.cn/test-fixture.pdf",
        title="中文双页模拟政策（非真实政策）",
        retrieved_at=BASE,
        provenance_note="Synthetic fixture; not an official policy document.",
    )
    data.update(changes)
    return PdfMetadata(**data)


def artifact_from_fixture(fid="f" * 32, **changes):
    body = FIXTURE_PDF.read_bytes()
    return PdfArtifact(
        fetch_id=fid,
        metadata=metadata(**changes),
        raw_sha256=_sha(body),
        body_size=len(body),
        body_base64=base64.b64encode(body).decode("ascii"),
    )


def commit(store, raw):
    return ingest_pdf(store, raw, approved_sha256=preview_pdf(raw).review_sha256)


@pytest.fixture
def store(tmp_path):
    return VersionedPolicyStore(tmp_path / "pdf.db")


# ==========================================================================
# 1. 旧 HTML 数据库重放回归（重构前后兼容性）
# ==========================================================================
@pytest.mark.skipif(
    not REAL_HTML_DB.is_file() or not PROFILES.is_file(),
    reason="缺少真实 HTML 版本库或解析配置，跳过兼容性回归",
)
class TestLegacyHtmlDatabaseCompatibility:
    """用备份副本验证 versioning.py 重构未破坏既有 HTML 数据。"""

    @pytest.fixture
    def backup_db(self, tmp_path):
        if not (NDRC_RAW.is_file() and NEA_RAW.is_file()):
            pytest.skip("缺少真实 HTML 原始文件")
        target = tmp_path / "legacy_backup.db"
        shutil.copy2(REAL_HTML_DB, target)
        return target

    def test_backup_opens_without_migration(self, backup_db):
        store = VersionedPolicyStore(backup_db)
        counts = store.counts()
        assert counts == dict(policies=2, versions=2, fetches=2, documents=2)

    def test_old_fetch_id_still_replayed(self, backup_db):
        store = VersionedPolicyStore(backup_db)
        before = store.counts()
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))

        for raw_path, key in (
            (NDRC_RAW, "ndrc/market_rules"),
            (NEA_RAW, "nea/registration_rules"),
        ):
            raw = load_raw(raw_path)
            result = store.ingest(
                raw, ParseProfile.model_validate(profiles[key])
            )
            assert result.action == "replayed", f"{key} 应为 replayed"
            assert result.version == 1

        assert store.counts() == before, "重放不得改变任何计数"

    def test_documents_still_readable(self, backup_db):
        store = VersionedPolicyStore(backup_db)
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        raw = load_raw(NDRC_RAW)
        result = store.ingest(
            raw, ParseProfile.model_validate(profiles["ndrc/market_rules"])
        )

        doc = PolicyRepository(backup_db).get(result.document_id)
        assert doc is not None
        assert "电力市场运行基本规则" in doc.title
        assert len(doc.body_text) > 3000

    def test_legacy_input_sha_unchanged(self, backup_db):
        """重构后旧记录的 input_sha256 必须仍是 HTML 公式算出的值。"""
        store = VersionedPolicyStore(backup_db)
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))

        conn = sqlite3.connect(backup_db)
        conn.row_factory = sqlite3.Row
        before = {
            row["fetch_id"]: row["input_sha256"]
            for row in conn.execute("SELECT fetch_id, input_sha256 FROM policy_fetches")
        }
        conn.close()

        for raw_path, key in (
            (NDRC_RAW, "ndrc/market_rules"),
            (NEA_RAW, "nea/registration_rules"),
        ):
            raw = load_raw(raw_path)
            profile = ParseProfile.model_validate(profiles[key])
            # 重放成功即证明指纹未变
            assert store.ingest(raw, profile).action == "replayed"
            assert before[raw.fetch_id] == self._fingerprint(raw, profile)

    @staticmethod
    def _fingerprint(raw, profile):
        from powercopy.data.parser import parse_article, to_policy_document
        from powercopy.data.versioning import _json_hash, _utc, content_hash

        doc = to_policy_document(raw, parse_article(raw, profile))
        return _json_hash(
            {
                "source_key": doc.source_key,
                "source_url": str(doc.source_url),
                "target_key": raw.target_key,
                "retrieved_at": _utc(raw.retrieved_at),
                "raw_sha256": _sha(raw.body_bytes()),
                "content_sha256": content_hash(doc),
                "content_kind": raw.content_kind,
                "status_code": raw.status_code,
                "content_type": raw.content_type,
                "charset": raw.charset,
                "profile": profile.model_dump(mode="json"),
            }
        )

    def test_pdf_and_html_coexist(self, backup_db):
        """旧 HTML 库可以继续接收 PDF，两者共用同一套版本表。"""
        store = VersionedPolicyStore(backup_db)
        before = store.counts()

        result = commit(store, artifact_from_fixture())
        assert result.action == "created"

        after = store.counts()
        assert after["documents"] == before["documents"] + 1
        assert after["policies"] == before["policies"] + 1


# ==========================================================================
# 2. 中文模拟 PDF 逐页与页码映射
# ==========================================================================
class TestChineseFixtureExtraction:
    def test_two_pages_extracted(self):
        preview = preview_pdf(artifact_from_fixture())
        assert len(preview.pages) == 2
        assert [p.number for p in preview.pages] == [1, 2]

    def test_chinese_text_correct(self):
        preview = preview_pdf(artifact_from_fixture())
        assert "第一条 交易周期为15分钟。" in preview.pages[0].text
        assert "第二条 电价为3元/千瓦时。" in preview.pages[1].text
        assert "测试材料 非真实政策" in preview.pages[0].text

    def test_numbers_and_units_preserved(self):
        preview = preview_pdf(artifact_from_fixture())
        full = preview.document.body_text
        for token in ("15分钟", "3元/千瓦时", "第一条", "第二条"):
            assert token in full, token

    def test_page_ranges_map_exactly(self):
        preview = preview_pdf(artifact_from_fixture())
        body = preview.document.body_text
        for page in preview.pages:
            assert body[page.start:page.end] == page.text

    def test_page_numbers_are_one_based_and_contiguous(self):
        preview = preview_pdf(artifact_from_fixture())
        numbers = [p.number for p in preview.pages]
        assert numbers == list(range(1, len(preview.pages) + 1))

    def test_page_break_separator_present(self):
        preview = preview_pdf(artifact_from_fixture())
        assert PAGE_BREAK in preview.document.body_text
        assert preview.document.body_text.count(PAGE_BREAK) == len(preview.pages) - 1

    def test_ranges_skip_separator(self):
        """页区间之间应跳过 \n\f\n 分隔符。"""
        preview = preview_pdf(artifact_from_fixture())
        first, second = preview.pages
        assert second.start == first.end + len(PAGE_BREAK)


# ==========================================================================
# 3. 原始文件、Base64 与哈希
# ==========================================================================
class TestArtifactIntegrity:
    def test_import_saves_file(self, tmp_path):
        path = import_pdf(FIXTURE_PDF, metadata(), tmp_path / "raw_pdf")
        assert path.is_file()
        assert path.suffix == ".json"
        assert (
            load_pdf_artifact(path).body_bytes() == FIXTURE_PDF.read_bytes()
        )

    def test_base64_roundtrip(self):
        artifact = artifact_from_fixture()
        assert artifact.body_bytes() == FIXTURE_PDF.read_bytes()

    def test_raw_sha256_matches(self):
        artifact = artifact_from_fixture()
        assert artifact.raw_sha256 == _sha(FIXTURE_PDF.read_bytes())

    def test_tampered_sha_rejected(self):
        good = artifact_from_fixture()
        bad = good.model_copy(update={"raw_sha256": "0" * 64})
        with pytest.raises(PdfIngestError, match="RAW_INTEGRITY_MISMATCH"):
            bad.body_bytes()

    def test_tampered_size_rejected(self):
        good = artifact_from_fixture()
        bad = good.model_copy(update={"body_size": good.body_size - 1})
        with pytest.raises(PdfIngestError, match="RAW_INTEGRITY_MISMATCH"):
            bad.body_bytes()

    def test_source_metadata_roundtrip(self):
        artifact = artifact_from_fixture()
        meta = artifact.metadata
        assert meta.source_key == "nea_east"
        assert str(meta.source_url) == "https://hdj.nea.gov.cn/test-fixture.pdf"
        assert meta.provenance_note
        assert meta.published_on is None
        assert meta.issuer is None


# ==========================================================================
# 4. 拒绝场景
# ==========================================================================
class TestRejection:
    def test_not_pdf_magic(self):
        body = b"MZ\x90\x00 not a pdf"
        bad = PdfArtifact(
            fetch_id="a" * 32,
            metadata=metadata(),
            raw_sha256=_sha(body),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode("ascii"),
        )
        with pytest.raises(PdfIngestError, match="NOT_PDF"):
            bad.body_bytes()

    def test_oversize_artifact_field(self):
        with pytest.raises(Exception):
            PdfArtifact(
                fetch_id="a" * 32,
                metadata=metadata(),
                raw_sha256="0" * 64,
                body_size=MAX_BYTES + 1,
                body_base64="AAA=",
            )

    @pytest.mark.parametrize(
        "url",
        [
            "https://hdj.nea.gov.cn.evil.invalid/a.pdf",
            "http://hdj.nea.gov.cn/a.pdf",
            "https://user:pw@hdj.nea.gov.cn/a.pdf",
            "https://hdj.nea.gov.cn:8443/a.pdf",
            "https://other.gov.cn/a.pdf",
        ],
    )
    def test_unapproved_source(self, url):
        with pytest.raises(PdfIngestError, match="UNAPPROVED_SOURCE_URL"):
            preview_pdf(artifact_from_fixture(source_url=url))

    def test_unknown_source_key(self):
        with pytest.raises(PdfIngestError, match="UNAPPROVED_SOURCE_URL"):
            preview_pdf(artifact_from_fixture(source_key="unknown_agency"))

    def test_invalid_acquisition_kind(self):
        artifact = artifact_from_fixture().model_copy(
            update={"acquisition_kind": "auto_crawl"}
        )
        with pytest.raises(PdfIngestError, match="INVALID_ACQUISITION_KIND"):
            artifact.body_bytes()

    def test_invalid_base64(self):
        artifact = artifact_from_fixture().model_copy(
            update={"body_base64": "!!!not-base64!!!"}
        )
        with pytest.raises(PdfIngestError, match="INVALID_BASE64"):
            artifact.body_bytes()


# ==========================================================================
# 5. 审核指纹机制
# ==========================================================================
class TestReviewFingerprint:
    def test_preview_does_not_write(self, tmp_path):
        db = tmp_path / "should_not_exist.db"
        store = VersionedPolicyStore(db)
        before = store.counts()

        preview_pdf(artifact_from_fixture())
        assert store.counts() == before
        assert before == dict(policies=0, versions=0, fetches=0, documents=0)

    def test_missing_approval_rejected(self, store):
        with pytest.raises(PdfIngestError, match="REVIEW_REQUIRED_OR_STALE"):
            ingest_pdf(store, artifact_from_fixture(), approved_sha256="")
        assert store.counts()["documents"] == 0

    def test_wrong_approval_rejected(self, store):
        with pytest.raises(PdfIngestError, match="REVIEW_REQUIRED_OR_STALE"):
            ingest_pdf(
                store, artifact_from_fixture(), approved_sha256="0" * 64
            )
        assert store.counts()["documents"] == 0

    def test_stale_after_metadata_change(self, store):
        artifact = artifact_from_fixture()
        old_review = preview_pdf(artifact).review_sha256

        changed = artifact.model_copy(
            update={"metadata": metadata(title="被改过的标题")}
        )
        with pytest.raises(PdfIngestError, match="REVIEW_REQUIRED_OR_STALE"):
            ingest_pdf(store, changed, approved_sha256=old_review)
        assert store.counts()["documents"] == 0

    def test_review_sha_binds_parser_version(self):
        preview = preview_pdf(artifact_from_fixture())
        assert preview.parser_version.startswith("pdf-text-v1/pypdf-")

    def test_correct_approval_commits(self, store):
        artifact = artifact_from_fixture()
        review = preview_pdf(artifact).review_sha256
        result = ingest_pdf(store, artifact, approved_sha256=review)
        assert result.action == "created"


# ==========================================================================
# 6. 版本、去重与幂等
# ==========================================================================
class TestVersioning:
    def test_created_updated_unchanged_replayed(self, store):
        a = artifact_from_fixture(fid="a" * 32)
        r1 = commit(store, a)
        assert r1.action == "created" and r1.version == 1

        # 相同内容、新 fetch_id、更晚时间 → unchanged
        b = artifact_from_fixture(
            fid="b" * 32, retrieved_at=BASE + timedelta(hours=1)
        )
        r2 = commit(store, b)
        assert r2.action == "unchanged"
        assert r2.document_id == r1.document_id

        # 同一 fetch_id 重放 → replayed
        assert commit(store, a).action == "replayed"

    def test_idempotent_fetch_conflict(self, store):
        a = artifact_from_fixture(fid="a" * 32)
        commit(store, a)

        # 同 fetch_id 但标题被改 → 指纹不同 → 冲突
        changed = a.model_copy(
            update={"metadata": metadata(title="同 id 不同内容")}
        )
        with pytest.raises(VersionError, match="FETCH_ID_CONFLICT"):
            commit(store, changed)

    def test_transaction_rollback(self, store):
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER fail_pdf BEFORE INSERT ON policy_fetches "
                "BEGIN SELECT RAISE(ABORT, 'forced'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            commit(store, artifact_from_fixture())
        assert store.counts() == dict(
            policies=0, versions=0, fetches=0, documents=0
        )


# ==========================================================================
# 7. 已有查询层可读 PDF
# ==========================================================================
class TestQueryServiceOnPdf:
    def test_search_finds_pdf_document(self, store):
        artifact = artifact_from_fixture()
        result = commit(store, artifact)
        service = PolicyQueryService(store.db_path)

        found = service.search_policy("", "模拟政策")
        assert found["returned"] == 1
        assert found["items"][0]["policy_id"] == result.policy_id

    def test_detail_returns_page_separator(self, store):
        artifact = artifact_from_fixture()
        result = commit(store, artifact)
        service = PolicyQueryService(store.db_path)

        detail = service.get_policy_detail(result.policy_id, version=1)
        assert PAGE_BREAK in detail["body_text"]
        assert detail["raw_sha256"] == artifact.raw_sha256

    def test_body_text_matches_preview(self, store):
        artifact = artifact_from_fixture()
        preview = preview_pdf(artifact)
        result = commit(store, artifact)

        doc = PolicyRepository(store.db_path).get(result.document_id)
        assert doc.body_text == preview.document.body_text


# ==========================================================================
# 8. 真实 PDF 覆盖情况（诚实标注）
# ==========================================================================
class TestRealPdfCoverage:
    def test_no_real_pdf_in_project(self):
        """诚实记录：项目内没有经过授权的真实政策 PDF。"""
        raw_pdf = ROOT / "storage" / "raw_pdf"
        if not raw_pdf.is_dir():
            return
        for path in raw_pdf.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            note = data["metadata"]["provenance_note"]
            # 本项目所有已导入 PDF 都必须是明确的合成/测试样本
            assert "Synthetic" in note or "synthetic" in note, (
                f"{path.name} 来源说明看起来不是合成样本：{note}"
            )
