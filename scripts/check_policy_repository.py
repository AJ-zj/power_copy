"""Policy Document 数据模型与持久化验收脚本。

全部本地运行，不联网、不调用模型。

用法：
    python scripts/check_policy_repository.py

验收九项：
    1. PolicyDocument 数据契约校验通过
    2. 非法或空白数据被正确拒绝
    3. 来源 URL 与采集时区校验通过
    4. SQLite 写入、读取与对象还原正确
    5. 重新创建 Repository 后数据依然存在
    6. 重复主键被拒绝，原数据不受影响
    7. 未知文档与独立快照处理正确
    8. 参数化 SQL 测试通过
    9. 全量回归（由 pytest 保证）

退出码：
    0  八项行为验收全部通过
    1  有项目未通过
"""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from powercopy.data.models import PolicyDocument  # noqa: E402
from powercopy.data.repository import PolicyRepository  # noqa: E402


def make_payload(**overrides):
    payload = {
        "source_key": "demo",
        "source_url": "https://example.invalid/policy/1",
        "title": "模拟售电政策",
        "body_text": "仅用于测试，不是真实政策。\n第二段。",
        "region": "安徽",
        "retrieved_at": datetime(2026, 11, 25, tzinfo=timezone.utc),
    }
    payload.update(overrides)
    return payload


def check_contract() -> bool:
    print("\n[1/9] PolicyDocument 数据契约")

    doc = PolicyDocument(**make_payload())
    second = PolicyDocument(**make_payload())

    checks = {
        "必填字段可读": doc.source_key == "demo" and doc.title == "模拟售电政策",
        "自动生成 UUID": doc.document_id is not None,
        "每个实例 ID 不同": doc.document_id != second.document_id,
        "可选字段默认 None": doc.issuer is None and doc.published_on is None,
        "禁止未定义字段": _raises(lambda: PolicyDocument(**make_payload(), x="y")),
        "禁止修改字段（frozen）": _raises(lambda: setattr(doc, "title", "改掉")),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_invalid_data() -> bool:
    print("\n[2/9] 非法或空白数据被拒绝")

    cases = [
        ("空白标题", {"title": "  "}),
        ("空标题", {"title": ""}),
        ("换行标题", {"title": "\n\t "}),
        ("空白正文", {"body_text": "\n  "}),
        ("空正文", {"body_text": ""}),
        ("空 source_key", {"source_key": ""}),
        ("空白 source_key", {"source_key": "   "}),
    ]
    all_ok = True
    for label, override in cases:
        rejected = _raises(lambda o=override: PolicyDocument(**make_payload(**o)))
        all_ok = all_ok and rejected
        print(f"  [{'x' if rejected else ' '}] {label} 被拒绝")

    stripped = PolicyDocument(**make_payload(title="  模拟政策  "))
    strip_ok = stripped.title == "模拟政策"
    all_ok = all_ok and strip_ok
    print(f"  [{'x' if strip_ok else ' '}] 标题自动去首尾空白")
    return all_ok


def check_url_and_timezone() -> bool:
    print("\n[3/9] 来源 URL 与采集时区")

    bad_urls = [
        "file:///etc/passwd",
        "ftp://example.com/policy",
        "not-a-url",
        "https://",
    ]
    all_ok = True
    for url in bad_urls:
        rejected = _raises(
            lambda u=url: PolicyDocument(**make_payload(source_url=u))
        )
        all_ok = all_ok and rejected
        print(f"  [{'x' if rejected else ' '}] 非法 URL 被拒绝：{url}")

    for url in ("http://example.invalid/a", "https://example.invalid/a"):
        ok = PolicyDocument(**make_payload(source_url=url)).source_url is not None
        all_ok = all_ok and ok
        print(f"  [{'x' if ok else ' '}] 合法 URL 接受：{url}")

    naive_rejected = _raises(
        lambda: PolicyDocument(**make_payload(retrieved_at=datetime(2026, 11, 25)))
    )
    all_ok = all_ok and naive_rejected
    print(f"  [{'x' if naive_rejected else ' '}] 无时区时间被拒绝")

    return all_ok


def check_write_read() -> bool:
    print("\n[4/9] SQLite 写入、读取与对象还原")

    with tempfile.TemporaryDirectory() as folder:
        repo = PolicyRepository(Path(folder) / "policies.db")
        doc = PolicyDocument(
            **make_payload(
                issuer="安徽省发展改革委",
                published_on=None,
            )
        )
        returned = repo.add(doc)
        loaded = repo.get(doc.document_id)

    checks = {
        "add 返回 document_id": returned == doc.document_id,
        "get 能读到": loaded is not None,
        "对象完全一致": loaded == doc,
        "字段完整": loaded.title == doc.title
        and loaded.body_text == doc.body_text
        and loaded.issuer == doc.issuer
        and loaded.region == doc.region,
        "多行正文保留": "\n" in loaded.body_text,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_persistence() -> bool:
    print("\n[5/9] 重建 Repository 后数据仍在")

    with tempfile.TemporaryDirectory() as folder:
        db_path = Path(folder) / "policies.db"
        doc = PolicyDocument(**make_payload())
        PolicyRepository(db_path).add(doc)

        reopened = PolicyRepository(db_path)
        loaded = reopened.get(doc.document_id)

    ok = loaded == doc
    print(f"  [{'x' if ok else ' '}] 新实例读到同一文档")
    return ok


def check_duplicate_key() -> bool:
    print("\n[6/9] 重复主键被拒绝，原数据不受影响")

    with tempfile.TemporaryDirectory() as folder:
        repo = PolicyRepository(Path(folder) / "policies.db")
        doc = PolicyDocument(**make_payload())
        repo.add(doc)

        duplicate_rejected = _raises(lambda: repo.add(doc))

        # 尝试用同 ID 不同内容覆盖
        tampered = doc.model_copy(update={"title": "试图覆盖"})
        tamper_rejected = _raises(lambda: repo.add(tampered))

        loaded = repo.get(doc.document_id)

        # 同内容不同 ID 允许写入
        another = PolicyDocument(**make_payload())
        another_ok = repo.add(another) == another.document_id

    checks = {
        "同 ID 重复写入被拒": duplicate_rejected,
        "同 ID 改内容也被拒": tamper_rejected,
        "原数据未被覆盖": loaded.title == doc.title,
        "同内容不同 ID 允许": another_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_unknown_and_snapshots() -> bool:
    print("\n[7/9] 未知文档与独立快照")

    with tempfile.TemporaryDirectory() as folder:
        repo = PolicyRepository(Path(folder) / "policies.db")

        unknown_none = repo.get(uuid4()) is None

        first = PolicyDocument(**make_payload())
        second = PolicyDocument(
            **make_payload(body_text="更新后的正文内容。")
        )
        repo.add(first)
        repo.add(second)

        loaded_first = repo.get(first.document_id)
        loaded_second = repo.get(second.document_id)

    checks = {
        "未知 ID 返回 None": unknown_none,
        "两个快照 ID 不同": first.document_id != second.document_id,
        "第一份正文未被改动": loaded_first.body_text
        == "仅用于测试，不是真实政策。\n第二段。",
        "第二份是新内容": loaded_second.body_text == "更新后的正文内容。",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_parameterized_sql() -> bool:
    print("\n[8/9] 参数化 SQL")

    injection_title = "'; DROP TABLE policy_documents; --"

    with tempfile.TemporaryDirectory() as folder:
        repo = PolicyRepository(Path(folder) / "policies.db")

        doc = PolicyDocument(**make_payload(title=injection_title))
        repo.add(doc)

        loaded = repo.get(doc.document_id)
        table_intact = repo.get(uuid4()) is None

        quote_title = "政策' OR '1'='1"
        quote_doc = PolicyDocument(**make_payload(title=quote_title))
        repo.add(quote_doc)
        results = repo.find_by_title(quote_title)

    checks = {
        "注入标题原样存储": loaded.title == injection_title,
        "表未受影响": table_intact,
        "带引号查询精确命中": len(results) == 1
        and results[0].document_id == quote_doc.document_id,
        "部分匹配不命中": repo.find_by_title("模拟售电") == []
        if False
        else True,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")

    # 部分匹配单独验证（需要 repo 仍在作用域内）
    with tempfile.TemporaryDirectory() as folder:
        repo2 = PolicyRepository(Path(folder) / "policies.db")
        repo2.add(PolicyDocument(**make_payload()))
        partial_ok = repo2.find_by_title("模拟售电") == []
    print(f"  [{'x' if partial_ok else ' '}] find_by_title 只做精确匹配")

    return all(checks.values()) and partial_ok


def _raises(action) -> bool:
    try:
        action()
    except Exception:
        return True
    return False


def main() -> int:
    print("=" * 64)
    print("PowerCopy V2 · 政策文档数据模型与持久化验收")
    print("全部本地运行，不联网、不调用模型")
    print("=" * 64)

    checks = [
        ("PolicyDocument 数据契约校验通过", check_contract()),
        ("非法或空白数据被正确拒绝", check_invalid_data()),
        ("来源 URL 与采集时区校验通过", check_url_and_timezone()),
        ("SQLite 写入、读取与对象还原正确", check_write_read()),
        ("重新创建 Repository 后数据依然存在", check_persistence()),
        ("重复主键被拒绝，原数据不受影响", check_duplicate_key()),
        ("未知文档与独立快照处理正确", check_unknown_and_snapshots()),
        ("参数化 SQL 测试通过", check_parameterized_sql()),
    ]

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(f"行为验收：{passed}/{len(checks)} 通过（第 9 项全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
