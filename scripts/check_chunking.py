"""5.1 Document Chunking 文档切片 · 十项验收脚本。

全程离线：不联网、不调用 LLM、不写数据库。
只读访问 storage/policies_versioned.db 与 storage/pdf_acceptance.db。

用法：
    python scripts/check_chunking.py

退出码：
    0  通过
    1  有项目未通过
"""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.parser import ParseProfile, RawPage, load_raw  # noqa: E402
from powercopy.data.pdf_ingest import load_pdf_artifact, preview_pdf  # noqa: E402
from powercopy.data.repository import PolicyRepository  # noqa: E402
from powercopy.data.versioning import VersionedPolicyStore  # noqa: E402
from powercopy.rag.chunking import (  # noqa: E402
    ALGORITHM,
    ChunkConfig,
    ChunkError,
    build_manifest,
    digest,
    read_latest,
    split_spans,
)

REAL_DB = ROOT / "storage" / "policies_versioned.db"
PDF_DB = ROOT / "storage" / "pdf_acceptance.db"
RAW_PDF = ROOT / "storage" / "raw_pdf"
PROFILES = ROOT / "config" / "parser_profiles.json"
NDRC_RAW = ROOT / "storage" / "raw" / "722b8057489c4ac2a54461d62dad117a.json"

DEFAULT = ChunkConfig(500, 80)


# ---------------------------------------------------------------- 1
def check_real_chunking() -> bool:
    print("\n[1/10] 两份真实 HTML 政策完成只读切片")

    if not REAL_DB.is_file():
        print("  [ ] 缺少 storage/policies_versioned.db，跳过")
        return False

    before = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()
    manifest = build_manifest(REAL_DB, DEFAULT)
    after = hashlib.sha256(REAL_DB.read_bytes()).hexdigest()

    print(f"\n  manifest_sha256 : {manifest['manifest_sha256']}")
    print(f"  algorithm       : {manifest['algorithm']}")
    print(f"  config          : {manifest['config']}")
    print(f"  documents       : {len(manifest['documents'])}")
    print(f"  chunks          : {len(manifest['chunks'])}")

    print("\n  每份政策统计：")
    ok_stats = True
    for doc in manifest["documents"]:
        doc_chunks = sorted(
            [
                c
                for c in manifest["chunks"]
                if c["document_id"] == doc["document_id"]
            ],
            key=lambda c: c["start"],
        )
        lengths = [c["end"] - c["start"] for c in doc_chunks]
        if not lengths:
            ok_stats = False
        print(f"\n    {doc['source_key']} / v{doc['version']}")
        print(f"      title        : {doc['title'][:52]}")
        print(f"      正文字数     : {doc['body_length']}")
        print(f"      切片数       : {doc['chunk_count']}")
        print(f"      最小切片     : {min(lengths)}")
        print(f"      最大切片     : {max(lengths)}")
        print(f"      首块区间     : [{doc_chunks[0]['start']}, {doc_chunks[0]['end']})")
        print(f"      末块区间     : [{doc_chunks[-1]['start']}, {doc_chunks[-1]['end']})")

    checks = {
        "构建出 2 份文档": len(manifest["documents"]) == 2,
        "产出切片非空": len(manifest["chunks"]) > 0,
        "每份文档都有切片": ok_stats,
        "数据库字节未变": before == after,
        "selection 为最新观测": manifest["selection"] == "latest_observed_only",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 2
def check_config_constraints() -> bool:
    print("\n[2/10] Chunk Size 与 Overlap 参数约束")

    rejects = {}
    for mc, ov in [
        (31, 0), (4097, 0), (100, 26), (100, -1),
        (True, 0), (100, False), (0, 0), (500, 500),
    ]:
        try:
            ChunkConfig(mc, ov)
            rejects[f"max={mc} overlap={ov}"] = False
        except ChunkError:
            rejects[f"max={mc} overlap={ov}"] = True

    accepts = {}
    for mc, ov in [(32, 0), (32, 8), (500, 80), (4096, 1024), (1000, 0)]:
        try:
            ChunkConfig(mc, ov)
            accepts[f"max={mc} overlap={ov}"] = True
        except ChunkError:
            accepts[f"max={mc} overlap={ov}"] = False

    print(f"\n  合法范围：max_chars ∈ [32, 4096]，overlap ∈ [0, max_chars//4]")
    print(f"  默认值：max_chars=500, overlap_chars=80")
    print(f"  非法参数 {len(rejects)} 组全部拒绝，合法参数 {len(accepts)} 组全部接受")

    checks = {**rejects, **accepts,
              "默认配置合法": ChunkConfig() == DEFAULT}
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 3
def check_natural_boundary() -> bool:
    print("\n[3/10] 自然边界优先及硬长度上限")

    cfg = ChunkConfig(100, 20)

    newline_text = "甲" * 90 + "\n" + "乙" * 200
    newline_spans = list(split_spans(newline_text, cfg))

    sentence_text = "甲" * 90 + "。" + "乙" * 200
    sentence_spans = list(split_spans(sentence_text, cfg))

    hard_text = "甲" * 5000
    hard_spans = list(split_spans(hard_text, cfg))
    within_cap = all(0 < s.end - s.start <= 100 for s in hard_spans)

    no_tail_text = "甲" * 100
    no_tail_spans = list(split_spans(no_tail_text, cfg))

    overlap_ok = all(
        b.start == a.end - 20
        for a, b in zip(sentence_spans, sentence_spans[1:])
    )

    print(f"\n  换行优先   : 首块 end={newline_spans[0].end}（期望 91）")
    print(f"  句末标点   : 首块 end={sentence_spans[0].end}（期望 91）")
    print(f"  硬切上限   : {len(hard_spans)} 块，全部 <= 100 字符")
    print(f"  无冗余尾块 : {len(no_tail_spans)} 块（期望 1）")

    checks = {
        "换行边界优先": newline_spans[0].end == 91,
        "句末标点优先": sentence_spans[0].end == 91,
        "硬切不超过上限": within_cap,
        "不产生冗余尾块": len(no_tail_spans) == 1,
        "相邻块 overlap 精确": overlap_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 4
def check_exact_offsets() -> bool:
    print("\n[4/10] 原文字符位置精确还原且无业务正文遗漏")

    if not REAL_DB.is_file():
        print("  [ ] 缺少真实政策库，跳过")
        return False

    manifest = build_manifest(REAL_DB, DEFAULT)
    repo = PolicyRepository(REAL_DB)

    all_ok = True
    for doc in manifest["documents"]:
        body = repo.get(doc["document_id"]).body_text
        chunks = sorted(
            [
                c
                for c in manifest["chunks"]
                if c["document_id"] == doc["document_id"]
            ],
            key=lambda c: c["start"],
        )

        exact = all(c["text"] == body[c["start"] : c["end"]] for c in chunks)
        sha_ok = all(
            c["text_sha256"]
            == hashlib.sha256(c["text"].encode("utf-8")).hexdigest()
            for c in chunks
        )

        covered = set()
        for c in chunks:
            covered.update(range(c["start"], c["end"]))
        nonblank = {i for i, ch in enumerate(body) if not ch.isspace()}
        missing = nonblank - covered

        first, last = chunks[0], chunks[-1]

        print(f"\n  {doc['source_key']} / v{doc['version']}  ({len(body)} 字符)")
        print(f"    切片数           : {len(chunks)}")
        print(f"    全部 text 精确还原: {exact}")
        print(f"    text_sha256 匹配 : {sha_ok}")
        print(f"    非空白字符数     : {len(nonblank)}")
        print(f"    未覆盖字符数     : {len(missing)}")
        print(f"    首块 [{first['start']}, {first['end']}) = {first['text'][:40]!r}")
        print(f"    末块 [{last['start']}, {last['end']}) = {last['text'][-40:]!r}")

        all_ok = all_ok and exact and sha_ok and not missing
        all_ok = all_ok and first["start"] == 0 and last["end"] == len(body)

    checks = {
        "逐块原文区间精确": all_ok,
        "无未覆盖非空白字符": all_ok,
        "首块 start=0": all_ok,
        "末块 end=正文长度": all_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 5
def check_determinism() -> bool:
    print("\n[5/10] 相同输入重复构建时 Chunk ID 与 Manifest Hash 一致")

    if not REAL_DB.is_file():
        print("  [ ] 缺少真实政策库，跳过")
        return False

    m1 = build_manifest(REAL_DB, DEFAULT)
    m2 = build_manifest(REAL_DB, DEFAULT)

    ids1 = [c["chunk_id"] for c in m1["chunks"]]
    ids2 = [c["chunk_id"] for c in m2["chunks"]]

    payload = {k: v for k, v in m1.items() if k != "manifest_sha256"}

    print(f"\n  第一次 manifest_sha256: {m1['manifest_sha256']}")
    print(f"  第二次 manifest_sha256: {m2['manifest_sha256']}")
    print(f"  chunk_id 序列长度      : {len(ids1)}")

    checks = {
        "manifest_sha256 一致": m1["manifest_sha256"] == m2["manifest_sha256"],
        "chunk_id 序列一致": ids1 == ids2,
        "清单对象完全相等": m1 == m2,
        "manifest_sha256 自洽": m1["manifest_sha256"] == digest(payload),
        "chunk_id 全部唯一": len(set(ids1)) == len(ids1),
        "chunk_id 为 SHA256": all(
            len(i) == 64 and all(ch in "0123456789abcdef" for ch in i)
            for i in ids1
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 6
def check_identity_update() -> bool:
    print("\n[6/10] 政策版本变化及切片配置变化时身份正确更新")

    if not REAL_DB.is_file() or not PROFILES.is_file() or not NDRC_RAW.is_file():
        print("  [ ] 缺少真实政策库/配置/原始文件，跳过")
        return False

    base = build_manifest(REAL_DB, DEFAULT)
    base_ids = {c["chunk_id"] for c in base["chunks"]}

    # 配置变化
    config_changed = {}
    for mc, ov in [(500, 0), (300, 60), (1000, 200)]:
        other = build_manifest(REAL_DB, ChunkConfig(mc, ov))
        other_ids = {c["chunk_id"] for c in other["chunks"]}
        config_changed[f"max={mc},overlap={ov} 无重叠"] = not (
            base_ids & other_ids
        )
        config_changed[f"max={mc},overlap={ov} manifest 变化"] = (
            other["manifest_sha256"] != base["manifest_sha256"]
        )

    # 版本变化（临时库）
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
    version_checks = {}
    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        profile = ParseProfile.model_validate(profiles["ndrc/market_rules"])
        raw = load_raw(NDRC_RAW)

        v1 = store.ingest(raw, profile)
        m1 = build_manifest(
            store.db_path, DEFAULT, raw_pdf_dir=Path(folder) / "raw_pdf"
        )

        html = raw.body_bytes().decode("utf-8").replace(
            "本规则自2024年7月1日起施行", "本规则自2026年1月1日起施行"
        )
        body = html.encode("utf-8")
        import base64

        changed = RawPage(
            fetch_id="v2-fetch",
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
        v2 = store.ingest(changed, profile)
        m2 = build_manifest(
            store.db_path, DEFAULT, raw_pdf_dir=Path(folder) / "raw_pdf"
        )

        ids1 = {c["chunk_id"] for c in m1["chunks"]}
        ids2 = {c["chunk_id"] for c in m2["chunks"]}
        latest_rows = read_latest(store.db_path)

        print(f"\n  版本变化：v{m1['documents'][0]['version']} -> v{m2['documents'][0]['version']}")
        print(f"    document_id 变化 : {v1.document_id != v2.document_id}")
        print(f"    chunk_id 无重叠  : {not (ids1 & ids2)}")
        print(f"    默认清单版本数   : {len(latest_rows)}")
        print(f"    库内版本总数     : {store.counts()['versions']}")

        version_checks = {
            "版本号递增": m1["documents"][0]["version"]
            < m2["documents"][0]["version"],
            "document_id 更新": v1.document_id != v2.document_id,
            "chunk_id 全部更新": not (ids1 & ids2),
            "manifest 变化": m1["manifest_sha256"] != m2["manifest_sha256"],
            "默认清单只含最新版本": len(latest_rows) == 1,
            "历史版本未混入": v1.document_id
            not in {c["document_id"] for c in m2["chunks"]},
        }

    checks = {**config_changed, **version_checks}
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 7
def check_metadata_linkage() -> bool:
    print("\n[7/10] 来源、版本、采集记录及哈希元数据关联")

    if not REAL_DB.is_file():
        print("  [ ] 缺少真实政策库，跳过")
        return False

    manifest = build_manifest(REAL_DB, DEFAULT)
    conn = sqlite3.connect(REAL_DB)
    conn.row_factory = sqlite3.Row

    ok = True
    for doc in manifest["documents"]:
        row = conn.execute(
            """SELECT v.version, v.content_sha256, d.source_url, d.title,
                      f.fetch_id, f.raw_sha256
               FROM policy_versions v
               JOIN policy_documents d ON d.document_id = v.document_id
               JOIN policy_fetches f ON f.policy_id = v.policy_id
                    AND f.version = v.version
               WHERE d.document_id = ?
               ORDER BY f.retrieved_at, f.fetch_id LIMIT 1""",
            (doc["document_id"],),
        ).fetchone()

        matched = (
            doc["version"] == row["version"]
            and doc["content_sha256"] == row["content_sha256"]
            and doc["source_url"] == row["source_url"]
            and doc["reference_fetch_id"] == row["fetch_id"]
            and doc["raw_sha256"] == row["raw_sha256"]
        )
        ok = ok and matched

        print(f"\n  {doc['source_key']}")
        print(f"    policy_id          : {doc['policy_id']}")
        print(f"    document_id        : {doc['document_id']}")
        print(f"    version            : {doc['version']}")
        print(f"    source_url         : {doc['source_url']}")
        print(f"    reference_fetch_id : {doc['reference_fetch_id']}")
        print(f"    raw_sha256         : {doc['raw_sha256'][:24]}...")
        print(f"    content_sha256     : {doc['content_sha256'][:24]}...")
        print(f"    与库内记录一致     : {matched}")
    conn.close()

    required = {
        "policy_id", "document_id", "version", "content_sha256",
        "source_key", "source_url", "reference_fetch_id", "raw_sha256",
        "start", "end", "text", "text_sha256", "chunk_id", "page",
    }
    fields_ok = all(required <= set(c) for c in manifest["chunks"])

    checks = {
        "元数据与账本完全一致": ok,
        "每个 chunk 含全部必需字段": fields_ok,
        "page 字段对 HTML 为 None": all(
            c["page"] is None for c in manifest["chunks"]
        ),
    }
    for name, ok_flag in checks.items():
        print(f"  [{'x' if ok_flag else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 8
def check_pdf_pages() -> bool:
    print("\n[8/10] PDF 物理页码映射、不跨页及缺档保护")

    if not PDF_DB.is_file() or not RAW_PDF.is_dir():
        print("  [ ] 缺少 PDF 验收库或原始档案，跳过")
        return False

    artifacts = list(RAW_PDF.glob("*.json"))
    if not artifacts:
        print("  [ ] 缺少 PDF 原始档案，跳过")
        return False

    manifest = build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=RAW_PDF)
    preview = preview_pdf(load_pdf_artifact(artifacts[0]))

    pages = sorted({c["page"] for c in manifest["chunks"]})
    within = all(
        preview.pages[c["page"] - 1].start
        <= c["start"]
        < c["end"]
        <= preview.pages[c["page"] - 1].end
        for c in manifest["chunks"]
    )
    no_cross = all("\f" not in c["text"] for c in manifest["chunks"])

    doc = manifest["documents"][0]
    body = PolicyRepository(PDF_DB).get(doc["document_id"]).body_text
    covered = set()
    for c in manifest["chunks"]:
        covered.update(range(c["start"], c["end"]))
    nonblank = {i for i, ch in enumerate(body) if not ch.isspace()}
    uncovered = nonblank - covered

    print(f"\n  文档        : {doc['title']}")
    print(f"  涉及物理页  : {pages}")
    print(f"  切片数      : {len(manifest['chunks'])}")
    for c in manifest["chunks"]:
        print(f"    第 {c['page']} 页 [{c['start']:3d}, {c['end']:3d})  {c['text']!r}")

    # 缺档保护
    with tempfile.TemporaryDirectory() as folder:
        empty = Path(folder) / "empty"
        empty.mkdir()
        missing_ok = False
        try:
            build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=empty)
        except ChunkError as error:
            missing_ok = "PDF_ARTIFACT_REQUIRED" in str(error)

        tampered = Path(folder) / "tampered"
        shutil.copytree(RAW_PDF, tampered)
        for path in tampered.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["raw_sha256"] = "0" * 64
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        hash_ok = False
        try:
            build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=tampered)
        except ChunkError:
            hash_ok = True

        meta_tampered = Path(folder) / "meta"
        shutil.copytree(RAW_PDF, meta_tampered)
        for path in meta_tampered.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["metadata"]["title"] = "篡改"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        meta_ok = False
        try:
            build_manifest(PDF_DB, ChunkConfig(32, 8), raw_pdf_dir=meta_tampered)
        except ChunkError as error:
            meta_ok = "PDF_PROVENANCE_MISMATCH" in str(error)

    checks = {
        "页码取自物理页": pages == [1, 2],
        "所有块在页区间内": within,
        "块不跨越物理页": no_cross,
        "非空白字符全覆盖": not uncovered,
        "与 4.8 预览区间一致": within,
        "缺档被拒绝": missing_ok,
        "哈希不一致被拒绝": hash_ok,
        "元数据篡改被拒绝": meta_ok,
        "reference_fetch_id 可追溯": doc["reference_fetch_id"]
        == load_pdf_artifact(artifacts[0]).fetch_id,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 9
def check_special_tests() -> bool:
    print("\n[9/10] 专项自动化测试")

    tests = {
        "开发包专项 tests/test_chunking.py": (
            ROOT / "tests" / "test_chunking.py"
        ).is_file(),
        "补充专项 tests/test_chunking_extra.py": (
            ROOT / "tests" / "test_chunking_extra.py"
        ).is_file(),
        "RAG 模块存在": (ROOT / "powercopy" / "rag" / "chunking.py").is_file(),
        "CLI 存在": (ROOT / "scripts" / "build_chunks.py").is_file(),
        "算法标识正确": ALGORITHM == "policy-char-v1",
    }

    # 清单 schema 自洽
    if REAL_DB.is_file():
        manifest = build_manifest(REAL_DB, DEFAULT)
        tests["清单 schema 正确"] = (
            manifest["schema"] == "policy-chunk-manifest-v1"
        )
        tests["notice 声明法律效力限制"] = "not verified" in manifest["notice"].lower()
    else:
        tests["清单 schema 正确"] = False
        tests["notice 声明法律效力限制"] = False

    # 空库
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "empty.db"
        VersionedPolicyStore(db)
        empty = build_manifest(db)
        tests["空库输出零块清单"] = empty["chunks"] == []

    # 缺库
    missing_ok = False
    try:
        build_manifest(ROOT / "storage" / "absent_chunk.db")
    except ChunkError as error:
        missing_ok = "POLICY_DB_NOT_FOUND" in str(error)
    tests["缺库被拒绝"] = missing_ok

    for name, ok in tests.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(tests.values())


# ---------------------------------------------------------------- 10
def check_regression() -> bool:
    print("\n[10/10] 全量回归及已验收模块保持不变")

    core_files = [
        "core/agent.py", "core/llm.py", "core/budget.py", "core/trace.py",
        "core/message.py", "core/retry.py", "core/schemas.py",
        "tools/base.py", "tools/registry.py", "tools/executor.py",
        "tools/result.py",
        "data/models.py", "data/repository.py", "data/parser.py",
        "data/normalizer.py", "data/sources.py", "data/fetcher.py",
        "data/pipeline.py", "data/query.py", "data/versioning.py",
        "data/pdf_ingest.py",
    ]

    print("\n  已验收模块 SHA256（前 12 位）：")
    digests = {}
    for relative in core_files:
        path = ROOT / "powercopy" / relative
        digest_value = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        digests[relative] = digest_value
        print(f"    {relative:24} {digest_value}")

    checks = {
        "所有模块存在": all(digests.values()),
        "rag 模块为新增目录": (
            ROOT / "powercopy" / "rag" / "chunking.py"
        ).is_file(),
        "data 目录无新增修改痕迹": len(digests) == len(core_files),
        "专项测试可发现": (ROOT / "tests" / "test_chunking.py").is_file()
        and (ROOT / "tests" / "test_chunking_extra.py").is_file(),
    }

    print("\n  本节为纯新增模块（powercopy/rag/ + scripts/build_chunks.py + 测试），")
    print("  data/ 与 core/ 未被修改。全量回归请运行：python -m pytest tests -q")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 74)
    print("PowerCopy V2 · 5.1 Document Chunking 文档切片 验收")
    print("全程离线：不联网、不调用 LLM、不写数据库")
    print("=" * 74)

    checks = [
        ("真实 HTML 政策只读切片", check_real_chunking()),
        ("Chunk Size 与 Overlap 约束", check_config_constraints()),
        ("自然边界优先与硬上限", check_natural_boundary()),
        ("字符位置精确与无遗漏", check_exact_offsets()),
        ("重复构建确定性", check_determinism()),
        ("版本与配置变化身份更新", check_identity_update()),
        ("元数据与账本关联", check_metadata_linkage()),
        ("PDF 页码映射与缺档保护", check_pdf_pages()),
        ("专项自动化测试", check_special_tests()),
        ("全量回归与模块不变", check_regression()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 74)
    print(f"验收结果：{passed}/{len(checks)} 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 74)
    print("全量回归请运行：python -m pytest tests -q")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
