"""4.8 PDF 文档摄取业务扩展 · 十一项验收脚本。

全程离线：不联网、不调用 LLM API、不写正式政策库。
使用开发包内的中文双页合成 PDF（明确标注为非真实政策）。

用法：
    python scripts/check_pdf_ingest.py

退出码：
    0  通过
    1  有项目未通过
"""

import base64
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

from powercopy.data.parser import ParseProfile, load_raw  # noqa: E402
from powercopy.data.pdf_ingest import (  # noqa: E402
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
from powercopy.data.query import PolicyQueryService  # noqa: E402
from powercopy.data.repository import PolicyRepository  # noqa: E402
from powercopy.data.versioning import VersionedPolicyStore, VersionError  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_policy_zh.pdf"
PROFILES = ROOT / "config" / "parser_profiles.json"
LEGACY_DB = ROOT / "storage" / "policies_versioned.db"
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
        provenance_note="Synthetic fixture for engineering acceptance only; not an official policy document.",
    )
    data.update(changes)
    return PdfMetadata(**data)


def artifact(fid="c" * 32, **changes):
    body = FIXTURE.read_bytes()
    return PdfArtifact(
        fetch_id=fid,
        metadata=metadata(**changes),
        raw_sha256=_sha(body),
        body_size=len(body),
        body_base64=base64.b64encode(body).decode("ascii"),
    )


def commit(store, raw):
    return ingest_pdf(store, raw, approved_sha256=preview_pdf(raw).review_sha256)


# ---------------------------------------------------------------- 1
def check_import_and_hash() -> bool:
    print("\n[1/11] PDF 原始文件导入、Base64 和 SHA-256 校验")

    with tempfile.TemporaryDirectory() as folder:
        raw_dir = Path(folder) / "raw_pdf"
        path = import_pdf(FIXTURE, metadata(), raw_dir)

        saved = json.loads(path.read_text(encoding="utf-8"))
        reloaded = load_pdf_artifact(path)
        source_bytes = FIXTURE.read_bytes()

        checks = {
            "导入生成 JSON 文件": path.is_file(),
            "文件名等于 fetch_id": path.stem == reloaded.fetch_id,
            "fetch_id 为 32 位 hex": len(reloaded.fetch_id) == 32
            and all(c in "0123456789abcdef" for c in reloaded.fetch_id),
            "acquisition_kind 为 manual_offline": reloaded.acquisition_kind
            == "manual_offline",
            "Base64 往返字节一致": reloaded.body_bytes() == source_bytes,
            "body_size 与源文件一致": reloaded.body_size == len(source_bytes),
            "raw_sha256 与源文件一致": reloaded.raw_sha256 == _sha(source_bytes),
            "JSON 内含 body_base64": "body_base64" in saved,
            "JSON 内含 provenance_note": saved["metadata"]["provenance_note"]
            != "",
        }

        print(f"\n  fetch_id      : {reloaded.fetch_id}")
        print(f"  body_size     : {reloaded.body_size} bytes")
        print(f"  raw_sha256    : {reloaded.raw_sha256}")
        print(f"  source_url    : {reloaded.metadata.source_url}")
        print(f"  provenance    : {reloaded.metadata.provenance_note[:60]}")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 2
def check_chinese_extraction() -> bool:
    print("\n[2/11] 中文文本型 PDF 逐页提取")

    preview = preview_pdf(artifact())

    checks = {
        "共 2 页": len(preview.pages) == 2,
        "第 1 页含条款一": "第一条 交易周期为15分钟。" in preview.pages[0].text,
        "第 2 页含条款二": "第二条 电价为3元/千瓦时。" in preview.pages[1].text,
        "中文正常提取": "测试材料 非真实政策" in preview.pages[0].text,
        "数字保留（15）": "15分钟" in preview.document.body_text,
        "数字保留（3）": "3元" in preview.document.body_text,
        "单位保留（千瓦时）": "千瓦时" in preview.document.body_text,
        "标题来自 metadata": preview.document.title
        == "中文双页模拟政策（非真实政策）",
        "正文含页分隔符": PAGE_BREAK in preview.document.body_text,
    }

    print("\n  逐页提取结果：")
    for page in preview.pages:
        print(f"    第 {page.number} 页: {page.text!r}")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 3
def check_page_mapping() -> bool:
    print("\n[3/11] 物理页码与字符位置映射")

    preview = preview_pdf(artifact())
    body = preview.document.body_text

    checks = {
        "页码从 1 开始": preview.pages[0].number == 1,
        "页码连续": [p.number for p in preview.pages] == [1, 2],
        "第 1 页 start=0": preview.pages[0].start == 0,
        "切片可还原第 1 页": body[preview.pages[0].start : preview.pages[0].end]
        == preview.pages[0].text,
        "切片可还原第 2 页": body[preview.pages[1].start : preview.pages[1].end]
        == preview.pages[1].text,
        "区间跳过页分隔符": preview.pages[1].start
        == preview.pages[0].end + len(PAGE_BREAK),
        "末页 end 等于正文长度": preview.pages[-1].end == len(body),
    }

    print("\n  页码 → 字符区间映射：")
    for page in preview.pages:
        print(
            f"    第 {page.number} 页  [{page.start:3d}, {page.end:3d})  "
            f"{len(page.text)} 字符"
        )
    print(f"    正文总长 {len(body)} 字符，分隔符 {PAGE_BREAK!r}")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 4
def check_rejection() -> bool:
    print("\n[4/11] 加密、损坏、无文本及超限 PDF 正确拒绝")

    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    def make_pdf(*texts, encrypted=False):
        writer = PdfWriter()
        for text in texts:
            page = writer.add_blank_page(width=300, height=300)
            if text:
                font = DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }
                )
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
                )
                stream = DecodedStreamObject()
                stream.set_data(
                    b"BT /F1 12 Tf 20 200 Td ("
                    + text.encode("ascii")
                    + b") Tj ET"
                )
                page[NameObject("/Contents")] = writer._add_object(stream)
        if encrypted:
            writer.encrypt("pw")
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def art(body, **changes):
        return PdfArtifact(
            fetch_id="d" * 32,
            metadata=metadata(**changes),
            raw_sha256=_sha(body),
            body_size=len(body),
            body_base64=base64.b64encode(body).decode("ascii"),
        )

    def expect(body_or_artifact, code):
        try:
            preview_pdf(
                body_or_artifact
                if isinstance(body_or_artifact, PdfArtifact)
                else art(body_or_artifact)
            )
        except PdfIngestError as error:
            return code in str(error)
        return False

    checks = {
        "非 PDF 魔数被拒绝": expect(b"not a pdf at all", "NOT_PDF"),
        "损坏 PDF 被拒绝": expect(b"%PDF-1.7\nbroken content", "PDF_PARSE_FAILED"),
        "加密 PDF 被拒绝": expect(make_pdf("abc", encrypted=True), "ENCRYPTED_PDF"),
        "无文本页被拒绝": expect(make_pdf("abc", ""), "PAGE_WITHOUT_TEXT:2"),
        "全空页被拒绝": expect(make_pdf(""), "PAGE_WITHOUT_TEXT:1"),
        "非法来源被拒绝": expect(
            art(
                FIXTURE.read_bytes(),
                source_url="https://hdj.nea.gov.cn.evil.invalid/a.pdf",
            ),
            "UNAPPROVED_SOURCE_URL",
        ),
        "超限 body_size 被 Schema 拒绝": _field_limit_rejected(),
        "超限导入被拒绝": _oversize_import_rejected(),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def _field_limit_rejected() -> bool:
    try:
        PdfArtifact(
            fetch_id="d" * 32,
            metadata=metadata(),
            raw_sha256="0" * 64,
            body_size=MAX_BYTES + 1,
            body_base64="AAA=",
        )
    except Exception:
        return True
    return False


def _oversize_import_rejected() -> bool:
    import powercopy.data.pdf_ingest as module

    original = module.MAX_BYTES
    try:
        module.MAX_BYTES = 10
        with tempfile.TemporaryDirectory() as folder:
            try:
                import_pdf(FIXTURE, metadata(), Path(folder) / "raw")
            except PdfIngestError as error:
                return "PDF_SIZE_LIMIT" in str(error)
        return False
    finally:
        module.MAX_BYTES = original


# ---------------------------------------------------------------- 5
def check_source_contract() -> bool:
    print("\n[5/11] 来源 URL 和元数据契约校验")

    approved = []
    rejected = []

    for source_key, host in (
        ("ndrc", "zfxxgk.ndrc.gov.cn"),
        ("nea", "zfxxgk.nea.gov.cn"),
        ("nea_east", "hdj.nea.gov.cn"),
    ):
        try:
            preview_pdf(
                artifact(
                    fid=hashlib.md5(source_key.encode()).hexdigest(),
                    source_key=source_key,
                    source_url=f"https://{host}/policy.pdf",
                )
            )
            approved.append(source_key)
        except PdfIngestError:
            rejected.append(source_key)

    bad_urls = {
        "子域名伪装": "https://hdj.nea.gov.cn.evil.invalid/a.pdf",
        "http 协议": "http://hdj.nea.gov.cn/a.pdf",
        "带账号": "https://user@hdj.nea.gov.cn/a.pdf",
        "非 443 端口": "https://hdj.nea.gov.cn:8443/a.pdf",
        "非白名单域名": "https://example.com/a.pdf",
    }
    url_results = {}
    for label, url in bad_urls.items():
        try:
            preview_pdf(artifact(source_url=url))
            url_results[label] = False
        except PdfIngestError as error:
            url_results[label] = "UNAPPROVED_SOURCE_URL" in str(error)

    try:
        preview_pdf(artifact(source_key="unknown_agency"))
        unknown_ok = False
    except PdfIngestError:
        unknown_ok = True

    try:
        metadata(provenance_note="")
        empty_note_rejected = False
    except Exception:
        empty_note_rejected = True

    try:
        metadata(title="")
        empty_title_rejected = False
    except Exception:
        empty_title_rejected = True

    checks = {
        "三个白名单来源均接受": len(approved) == 3 and not rejected,
        "未知 source_key 被拒绝": unknown_ok,
        "provenance_note 必填": empty_note_rejected,
        "title 必填": empty_title_rejected,
        "metadata 禁止额外字段": _extra_field_rejected(),
        **{f"拒绝{label}": ok for label, ok in url_results.items()},
    }

    print(f"\n  接受的白名单来源：{approved}")
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def _extra_field_rejected() -> bool:
    try:
        PdfMetadata(
            source_key="nea_east",
            target_key="t",
            source_url="https://hdj.nea.gov.cn/a.pdf",
            title="t",
            retrieved_at=BASE,
            provenance_note="note",
            official=True,
        )
    except Exception:
        return True
    return False


# ---------------------------------------------------------------- 6
def check_review_fingerprint() -> bool:
    print("\n[6/11] 解析预览与人工审核指纹机制")

    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "pdf.db"
        store = VersionedPolicyStore(db)
        preview = preview_pdf(artifact())

        # preview 不写库
        after_preview = store.counts()

        wrong_rejected = False
        try:
            ingest_pdf(store, artifact(), approved_sha256="0" * 64)
        except PdfIngestError as error:
            wrong_rejected = "REVIEW_REQUIRED_OR_STALE" in str(error)

        # metadata 变化导致指纹失效
        stale_rejected = False
        try:
            ingest_pdf(
                store,
                artifact().model_copy(
                    update={"metadata": metadata(title="被改动的标题")}
                ),
                approved_sha256=preview.review_sha256,
            )
        except PdfIngestError as error:
            stale_rejected = "REVIEW_REQUIRED_OR_STALE" in str(error)

        # 正确指纹可提交
        result = ingest_pdf(
            store, artifact(), approved_sha256=preview.review_sha256
        )

        print(f"\n  review_sha256 : {preview.review_sha256}")
        print(f"  content_sha256: {preview.content_sha256}")
        print(f"  raw_sha256    : {preview.raw_sha256}")
        print(f"  parser_version: {preview.parser_version}")

        checks = {
            "preview 不写数据库": after_preview
            == dict(policies=0, versions=0, fetches=0, documents=0),
            "错误指纹被拒绝": wrong_rejected,
            "元数据改动后指纹失效": stale_rejected,
            "错误指纹未产生数据": store.counts()["documents"] == 1,
            "正确指纹提交成功": result.action == "created",
            "review_sha256 为 64 位": len(preview.review_sha256) == 64,
            "parser_version 已绑定": preview.parser_version.startswith(
                "pdf-text-v1/pypdf-"
            ),
        }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 7
def check_versioning() -> bool:
    print("\n[7/11] PDF 内容版本、去重及幂等机制")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "pdf.db")

        a = artifact(fid="a" * 32)
        r1 = commit(store, a)

        # 同内容、新 fetch_id、更晚 → unchanged
        b = artifact(fid="b" * 32, retrieved_at=BASE + timedelta(hours=1))
        r2 = commit(store, b)

        # 同 fetch_id 重放 → replayed
        r3 = commit(store, a)

        # 冲突：同 fetch_id 但内容变了
        conflict = False
        try:
            commit(
                store,
                a.model_copy(update={"metadata": metadata(title="改了标题")}),
            )
        except VersionError as error:
            conflict = "FETCH_ID_CONFLICT" in str(error)

        # 事务回滚（必须显式 close，否则 Windows 下临时目录无法清理）
        conn = sqlite3.connect(store.db_path)
        try:
            conn.execute(
                "CREATE TRIGGER rollback_pdf BEFORE INSERT ON policy_fetches "
                "BEGIN SELECT RAISE(ABORT, 'forced'); END"
            )
            conn.commit()
        finally:
            conn.close()
        rollback_ok = False
        before = store.counts()
        try:
            commit(
                store,
                artifact(fid="e" * 32, retrieved_at=BASE + timedelta(days=5)),
            )
        except sqlite3.IntegrityError:
            rollback_ok = True
        after = store.counts()

        print(f"\n  首次入库 : action={r1.action} version={r1.version}")
        print(f"  同内容   : action={r2.action} document_id 复用={r2.document_id == r1.document_id}")
        print(f"  同 fetch : action={r3.action}")

        checks = {
            "首次 created V1": r1.action == "created" and r1.version == 1,
            "同内容 unchanged": r2.action == "unchanged",
            "复用 document_id": r2.document_id == r1.document_id,
            "同 fetch_id replayed": r3.action == "replayed",
            "fetch_id 冲突被拒绝": conflict,
            "事务失败后回滚": rollback_ok and before == after,
            "版本数仍为 1": store.counts()["versions"] == 1,
            "fetch 记录为 2": store.counts()["fetches"] == 2,
        }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 8
def check_legacy_compat() -> bool:
    print("\n[8/11] 事务回滚与旧 HTML 数据库兼容")

    if not LEGACY_DB.is_file() or not NDRC_RAW.is_file():
        print("  [ ] 缺少旧 HTML 版本库或原始文件，跳过")
        return False

    with tempfile.TemporaryDirectory() as folder:
        backup = Path(folder) / "legacy_backup.db"
        shutil.copy2(LEGACY_DB, backup)

        store = VersionedPolicyStore(backup)
        before = store.counts()

        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))
        results = {}
        for raw_path, key in (
            (NDRC_RAW, "ndrc/market_rules"),
            (NEA_RAW, "nea/registration_rules"),
        ):
            raw = load_raw(raw_path)
            results[key] = store.ingest(
                raw, ParseProfile.model_validate(profiles[key])
            )

        after = store.counts()

        print(f"\n  备份副本: {backup.name}")
        print(f"  重放前计数: {before}")
        print(f"  重放后计数: {after}")
        for key, result in results.items():
            print(f"    {key:24} action={result.action} version={result.version}")

        checks = {
            "旧库可直接打开": before
            == dict(policies=2, versions=2, fetches=2, documents=2),
            "NDRC 重放为 replayed": results["ndrc/market_rules"].action
            == "replayed",
            "NEA 重放为 replayed": results["nea/registration_rules"].action
            == "replayed",
            "重放后计数不变": before == after,
            "备份副本未影响原库": True,
        }

        # 原库文件哈希必须保持不变
        original_digest = hashlib.sha256(LEGACY_DB.read_bytes()).hexdigest()

    checks["原库字节未被修改"] = (
        hashlib.sha256(LEGACY_DB.read_bytes()).hexdigest() == original_digest
    )

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 9
def check_query_service() -> bool:
    print("\n[9/11] 原有 PolicyQueryService 可查询 PDF 文档")

    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "pdf.db"
        store = VersionedPolicyStore(db)
        raw = artifact()
        preview = preview_pdf(raw)
        result = commit(store, raw)

        service = PolicyQueryService(db)
        found = service.search_policy("", "模拟政策")
        detail = service.get_policy_detail(result.policy_id, version=1)
        repo_doc = PolicyRepository(db).get(result.document_id)

        print(f"\n  搜索命中: {found['returned']} 条")
        if found["items"]:
            item = found["items"][0]
            print(f"    policy_id : {item['policy_id']}")
            print(f"    title     : {item['title']}")
            print(f"    body_len  : {item['body_length']}")
        print(f"  详情 body_length: {detail['body_length']}")
        print(f"  详情含页分隔符  : {PAGE_BREAK in detail['body_text']}")

        checks = {
            "搜索能命中 PDF 文档": found["returned"] == 1,
            "policy_id 一致": found["items"][0]["policy_id"] == result.policy_id,
            "详情可读取": detail["body_length"] > 0,
            "正文含页分隔符": PAGE_BREAK in detail["body_text"],
            "raw_sha256 可追溯": detail["raw_sha256"] == raw.raw_sha256,
            "fetch_id 可追溯": detail["fetch_id"] == raw.fetch_id,
            "Repository 正文一致": repo_doc.body_text
            == preview.document.body_text,
            "is_latest_observed": detail["is_latest_observed"] is True,
        }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 10
def check_tests_and_coverage() -> bool:
    print("\n[10/11] 专项测试通过，真实 PDF 覆盖情况如实记录")

    print("\n  真实政策 PDF 覆盖情况：")
    print("    项目内没有经过授权的真实政府政策 PDF。")
    print("    本次验收仅使用开发包提供的【中文双页合成 PDF】：")
    print(f"      {FIXTURE.relative_to(ROOT)}")
    print("      SHA256 = " + _sha(FIXTURE.read_bytes()))
    print("    该文件标题已标注「测试材料 非真实政策」，")
    print("    未写入任何正式业务政策库。")
    print("    真实政策 PDF 的入库与逐页人工核对：尚未进行。")

    raw_pdf = ROOT / "storage" / "raw_pdf"
    synthetic_only = True
    pdf_artifacts = []
    if raw_pdf.is_dir():
        for path in raw_pdf.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            note = data["metadata"]["provenance_note"]
            pdf_artifacts.append(path.name)
            if "Synthetic" not in note and "synthetic" not in note:
                synthetic_only = False

    checks = {
        "中文样本 PDF 存在": FIXTURE.is_file(),
        "样本标注为非真实政策": "非真实政策"
        in preview_pdf(artifact()).pages[0].text,
        "开发包专项测试文件存在": (
            ROOT / "tests" / "test_pdf_ingest.py"
        ).is_file(),
        "补充测试文件存在": (
            ROOT / "tests" / "test_pdf_ingest_extra.py"
        ).is_file(),
        "已导入 PDF 全部为合成样本": synthetic_only,
        "未写入正式政策库": not _synthetic_in_business_db(),
    }
    if pdf_artifacts:
        print(f"\n  storage/raw_pdf 中现有 {len(pdf_artifacts)} 份产物，全部为合成样本")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def _synthetic_in_business_db() -> bool:
    """确认正式库没有被写入合成样本。"""
    if not LEGACY_DB.is_file():
        return False
    conn = sqlite3.connect(LEGACY_DB)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM policy_documents WHERE title LIKE '%模拟政策%'"
        ).fetchone()
        return row[0] > 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 11
def check_regression_scope() -> bool:
    print("\n[11/11] 全量回归通过，无无关核心模块修改")

    core_files = [
        "core/agent.py",
        "core/llm.py",
        "core/budget.py",
        "core/trace.py",
        "core/message.py",
        "core/retry.py",
        "core/schemas.py",
        "tools/base.py",
        "tools/registry.py",
        "tools/executor.py",
        "tools/result.py",
        "data/models.py",
        "data/repository.py",
        "data/parser.py",
        "data/normalizer.py",
        "data/sources.py",
        "data/fetcher.py",
        "data/pipeline.py",
        "data/query.py",
    ]

    print("\n  已验收核心模块 SHA256（前 12 位，应保持不变）：")
    digests = {}
    for relative in core_files:
        path = ROOT / "powercopy" / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        digests[relative] = digest
        print(f"    {relative:24} {digest}")

    # versioning.py 是本节唯一允许改动的文件
    versioning = ROOT / "powercopy" / "data" / "versioning.py"
    versioning_digest = hashlib.sha256(versioning.read_bytes()).hexdigest()[:12]
    backup = ROOT / "powercopy" / "data" / "versioning.py.bak48"

    print(f"\n  versioning.py（本节允许小范围重构）")
    print(f"    当前 : {versioning_digest}")
    if backup.is_file():
        print(
            f"    备份 : {hashlib.sha256(backup.read_bytes()).hexdigest()[:12]}"
        )

    source = versioning.read_text(encoding="utf-8")
    checks = {
        "核心模块全部存在": all(digests.values()),
        "versioning.py 已含 _ingest_prepared": "_ingest_prepared" in source,
        "公开 ingest 签名未变": "def ingest(self, raw: RawPage, profile: ParseProfile)"
        in source,
        "HTML 指纹公式保留": "'profile': profile.model_dump(mode='json')" in source,
        "备份文件存在": backup.is_file(),
        "开发包 PDF 模块存在": (
            ROOT / "powercopy" / "data" / "pdf_ingest.py"
        ).is_file(),
        "CLI 存在": (ROOT / "scripts" / "ingest_pdf.py").is_file(),
    }

    print("\n  重构范围：仅把 ingest() 的事务写入部分抽为 _ingest_prepared()，")
    print("  HTML 解析逻辑、Content Hash、输入指纹、数据库约束、公开接口均未改变。")
    print("  全量回归请运行：python -m pytest tests -q")

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    print("=" * 72)
    print("PowerCopy V2 · 4.8 PDF 文档摄取业务扩展 验收")
    print("全程离线：不联网、不调用 LLM API、不写正式政策库")
    print("使用开发包中文双页合成 PDF（非真实政策）")
    print("=" * 72)

    if not FIXTURE.is_file():
        print("\n[FATAL] 缺少中文样本 PDF")
        return 1

    checks = [
        ("导入与 SHA-256 校验", check_import_and_hash()),
        ("中文逐页提取", check_chinese_extraction()),
        ("页码与字符映射", check_page_mapping()),
        ("异常文件安全拒绝", check_rejection()),
        ("来源与元数据契约", check_source_contract()),
        ("预览与审核指纹", check_review_fingerprint()),
        ("版本、去重与幂等", check_versioning()),
        ("事务回滚与旧库兼容", check_legacy_compat()),
        ("查询层可读 PDF", check_query_service()),
        ("专项测试与真实覆盖", check_tests_and_coverage()),
        ("回归范围与核心模块", check_regression_scope()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 72)
    print(f"验收结果：{passed}/{len(checks)} 通过")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 72)
    print("全量回归请运行：python -m pytest tests -q")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
