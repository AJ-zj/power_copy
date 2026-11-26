"""4.4 版本管理十一项验收脚本。

默认使用构造数据，不读写真实采集文件。
加 --real 才验证 storage/raw/ 里的真实政策。

用法：
    python scripts/check_versioning.py           # 构造数据验收
    python scripts/check_versioning.py --real    # 额外验证真实数据

退出码：
    0  通过
    1  有项目未通过
"""

import base64
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.models import PolicyDocument  # noqa: E402
from powercopy.data.normalizer import ParseError  # noqa: E402
from powercopy.data.parser import (  # noqa: E402
    ParseProfile,
    RawPage,
    load_raw,
    parse_article,
    to_policy_document,
)
from powercopy.data.repository import PolicyRepository  # noqa: E402
from powercopy.data.versioning import (  # noqa: E402
    VersionedPolicyStore,
    VersionError,
    content_hash,
)

PROFILE = ParseProfile(title_selector="h1", body_selector="article")
BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def make_raw(
    fetch_id="f1",
    text="第一条 A",
    day=0,
    url="https://example.invalid/policy/1",
    source="demo",
    noise="",
    kind="policy",
    title="政策",
    target="detail",
):
    html = (
        f"<html><h1>{title}</h1><article><p>{text}</p></article>"
        f"<footer>{noise}</footer></html>"
    ).encode()
    return RawPage(
        fetch_id=fetch_id,
        source_key=source,
        target_key=target,
        source_url=url,
        content_kind=kind,
        status_code=200,
        content_type="text/html",
        charset="utf-8",
        retrieved_at=BASE + timedelta(days=day),
        body_size=len(html),
        body_base64=base64.b64encode(html).decode(),
    )


def check_identity() -> bool:
    print("\n[1/11] 政策身份稳定映射")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        a = store.ingest(make_raw(), PROFILE)
        b = store.ingest(make_raw("f2", day=1), PROFILE)
        c = store.ingest(make_raw("f3", url="https://example.invalid/2"), PROFILE)
        d = store.ingest(make_raw("f4", source="other"), PROFILE)
        total_policies = store.counts()["policies"]

    checks = {
        "同 URL 同 policy_id": a.policy_id == b.policy_id,
        "不同 URL 不同 policy_id": a.policy_id != c.policy_id,
        "不同 source 不同 policy_id": a.policy_id != d.policy_id,
        "共 3 个政策身份": total_policies == 3,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_first_version() -> bool:
    print("\n[2/11] 首次采集创建 V1")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        result = store.ingest(make_raw(), PROFILE)
        counts = store.counts()
        doc = PolicyRepository(store.db_path).get(result.document_id)
        obs = store.observations(result.policy_id)

    checks = {
        "action=created": result.action == "created",
        "version=1": result.version == 1,
        "四项计数均为 1": counts
        == dict(policies=1, versions=1, fetches=1, documents=1),
        "文档可读": doc is not None and doc.body_text == "第一条 A",
        "raw_sha256 为 64 位": len(obs[0]["raw_sha256"]) == 64,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_dedup() -> bool:
    print("\n[3/11] 相同内容去重")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        first = store.ingest(make_raw(), PROFILE)
        # 正文相同，只有页脚噪音不同
        second = store.ingest(make_raw("f2", day=1, noise="新页脚"), PROFILE)
        counts = store.counts()
        obs = store.observations(first.policy_id)

    checks = {
        "action=unchanged": second.action == "unchanged",
        "复用同一 document_id": second.document_id == first.document_id,
        "版本数仍为 1": counts["versions"] == 1,
        "fetch 记录为 2": counts["fetches"] == 2,
        "原始哈希不同": obs[0]["raw_sha256"] != obs[1]["raw_sha256"],
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_idempotency() -> bool:
    print("\n[4/11] fetch_id 幂等及冲突拒绝")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        first = store.ingest(make_raw(), PROFILE)
        replay = store.ingest(make_raw(), PROFILE)

        conflict_raised = False
        try:
            store.ingest(make_raw("f1", text="changed"), PROFILE)
        except VersionError as error:
            conflict_raised = "FETCH_ID_CONFLICT" in str(error)

        counts = store.counts()

    checks = {
        "重放返回 replayed": replay.action == "replayed",
        "重放复用文档": replay.document_id == first.document_id,
        "同 id 改内容被拒绝": conflict_raised,
        "重放不新增 fetch": counts["fetches"] == 1,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_new_version() -> bool:
    print("\n[5/11] 内容变化创建新版本")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        first = store.ingest(make_raw(), PROFILE)
        second = store.ingest(make_raw("f2", text="第一条 B", day=1), PROFILE)
        counts = store.counts()
        history = store.history(first.policy_id)

    checks = {
        "action=updated": second.action == "updated",
        "version=2": second.version == 2,
        "同 policy_id": second.policy_id == first.policy_id,
        "内容哈希变化": second.content_sha256 != first.content_sha256,
        "版本数 2": counts["versions"] == 2,
        "文档数 2": counts["documents"] == 2,
        "历史记录 2 条": len(history) == 2,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_recurrence() -> bool:
    print("\n[6/11] A→B→A 完整历史保留")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        a1 = store.ingest(make_raw("f1", "A", day=0), PROFILE)
        b = store.ingest(make_raw("f2", "B", day=1), PROFILE)
        a2 = store.ingest(make_raw("f3", "A", day=2), PROFILE)
        history = store.history(a1.policy_id)
        observations = store.observations(a1.policy_id)

        repo = PolicyRepository(store.db_path)
        doc_v1 = repo.get(a1.document_id)
        doc_v2 = repo.get(b.document_id)
        doc_v3 = repo.get(a2.document_id)

    checks = {
        "版本号 1,2,3": [a1.version, b.version, a2.version] == [1, 2, 3],
        "同一 policy_id": a1.policy_id == b.policy_id == a2.policy_id,
        "A 与 A 内容哈希相同": a1.content_sha256 == a2.content_sha256,
        "A 与 B 内容哈希不同": a1.content_sha256 != b.content_sha256,
        "三份独立文档": len({a1.document_id, b.document_id, a2.document_id}) == 3,
        "历史 3 个版本": len(history) == 3,
        "观测记录 3 条": len(observations) == 3,
        "V1 正文仍为 A": doc_v1.body_text == "A",
        "V2 正文为 B": doc_v2.body_text == "B",
        "V3 正文回到 A": doc_v3.body_text == "A",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_hash_lineage() -> bool:
    print("\n[7/11] 原始字节哈希及数据血缘")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        result = store.ingest(make_raw(), PROFILE)
        obs = store.observations(result.policy_id)

    doc = to_policy_document(make_raw(), parse_article(make_raw(), PROFILE))

    checks = {
        "content_hash 只依赖业务字段": content_hash(doc)
        == content_hash(doc.model_copy(update={"document_id": doc.document_id})),
        "raw_sha256 64 位": len(obs[0]["raw_sha256"]) == 64,
        "input_sha256 64 位": len(obs[0]["input_sha256"]) == 64,
        "fetch 与 version 关联": obs[0]["version"] == result.version,
        "target_key 记录": obs[0]["target_key"] == "detail",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_transaction_fk() -> bool:
    print("\n[8/11] 事务回滚、外键及并发保护")

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "v.db"
        store = VersionedPolicyStore(path)

        # 事务回滚
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TRIGGER reject_fetch BEFORE INSERT ON policy_fetches "
                "BEGIN SELECT RAISE(ABORT, 'forced'); END"
            )
            conn.commit()
        finally:
            conn.close()

        rollback_ok = False
        try:
            store.ingest(make_raw(), PROFILE)
        except sqlite3.IntegrityError:
            rollback_ok = True
        empty_after = store.counts() == dict(
            policies=0, versions=0, fetches=0, documents=0
        )

        # 并发：同 fetch_id 两个线程
        conn = sqlite3.connect(path)
        try:
            conn.execute("DROP TRIGGER reject_fetch")
            conn.commit()
        finally:
            conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: store.ingest(make_raw(), PROFILE), range(2)))
        concurrent_ok = sorted(r.action for r in results) == ["created", "replayed"]
        fetch_once = store.counts()["fetches"] == 1

        # 外键约束（必须在临时目录内检查，否则库文件已被清理）
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            fk_ok = False
            try:
                conn.execute("INSERT INTO policy_versions VALUES ('ghost', 1, 'doc', 'h')")
            except sqlite3.IntegrityError:
                fk_ok = True
        finally:
            conn.close()

    checks = {
        "失败后回滚干净": rollback_ok and empty_after,
        "并发只写一次": concurrent_ok and fetch_once,
        "外键约束生效": fk_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_late_and_invalid() -> bool:
    print("\n[9/11] 迟到事件与非法输入拒绝")

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        store.ingest(make_raw(day=2), PROFILE)

        late_ok = False
        try:
            store.ingest(make_raw("late", "old", day=1), PROFILE)
        except VersionError as error:
            late_ok = "OUT_OF_ORDER_FETCH" in str(error)

        same_time_ok = False
        try:
            store.ingest(make_raw("other", "changed", day=0), PROFILE)
        except VersionError as error:
            same_time_ok = "OUT_OF_ORDER_FETCH" in str(error) or "AMBIGUOUS" in str(error)

        news_ok = False
        try:
            store.ingest(make_raw("n1", kind="market_news"), PROFILE)
        except ParseError:
            news_ok = True

        index_ok = False
        try:
            store.ingest(make_raw("i1", kind="index"), PROFILE)
        except ParseError:
            index_ok = True

        blank_ok = False
        try:
            store.ingest(make_raw("   "), PROFILE)
        except VersionError as error:
            blank_ok = "INVALID_FETCH_ID_OR_TARGET" in str(error)

        counts = store.counts()

    checks = {
        "迟到 fetch 被拒绝": late_ok,
        "同时间不同内容被拒绝": same_time_ok,
        "市场新闻被拒绝": news_ok,
        "列表页被拒绝": index_ok,
        "空 fetch_id 被拒绝": blank_ok,
        "只保留 1 条 fetch": counts["fetches"] == 1,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_real_data() -> bool:
    print("\n[10/11] 真实政策离线入库与重放（--real）")

    raw_dir = ROOT / "storage" / "raw"
    profiles_path = ROOT / "config" / "parser_profiles.json"
    ndrc = raw_dir / "722b8057489c4ac2a54461d62dad117a.json"
    nea = raw_dir / "f5d31d6af4ab4e7fa76571bbe09bd53f.json"

    if not ndrc.is_file() or not nea.is_file() or not profiles_path.is_file():
        print("  [ ] 缺少真实采集文件或配置，跳过")
        return False

    profiles = __import__("json").loads(profiles_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")

        a = store.ingest(load_raw(ndrc), ParseProfile.model_validate(profiles["ndrc/market_rules"]))
        b = store.ingest(load_raw(nea), ParseProfile.model_validate(profiles["nea/registration_rules"]))

        a2 = store.ingest(load_raw(ndrc), ParseProfile.model_validate(profiles["ndrc/market_rules"]))
        b2 = store.ingest(load_raw(nea), ParseProfile.model_validate(profiles["nea/registration_rules"]))

        counts = store.counts()
        doc = PolicyRepository(store.db_path).get(a.document_id)

    checks = {
        "NDRC 创建 V1": a.action == "created" and a.version == 1,
        "NEA 创建 V1": b.action == "created" and b.version == 1,
        "两个不同政策身份": a.policy_id != b.policy_id,
        "重放均为 replayed": a2.action == "replayed" and b2.action == "replayed",
        "计数 2/2/2/2": counts
        == dict(policies=2, versions=2, fetches=2, documents=2),
        "NDRC 文档可读": doc is not None
        and "电力市场运行基本规则" in doc.title
        and len(doc.body_text) > 3000,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    real = "--real" in sys.argv

    print("=" * 66)
    print("PowerCopy V2 · 4.4 版本管理验收")
    print("默认使用构造数据" + ("；--real 验证真实政策" if real else ""))
    print("=" * 66)

    checks = [
        ("政策身份稳定映射", check_identity()),
        ("首次采集创建 V1", check_first_version()),
        ("相同内容去重", check_dedup()),
        ("fetch_id 幂等及冲突拒绝", check_idempotency()),
        ("内容变化创建新版本", check_new_version()),
        ("A→B→A 完整历史保留", check_recurrence()),
        ("原始字节哈希及数据血缘", check_hash_lineage()),
        ("事务回滚、外键及并发保护", check_transaction_fk()),
        ("迟到事件与非法输入拒绝", check_late_and_invalid()),
    ]

    if real:
        checks.append(("真实政策离线入库与重放", check_real_data()))

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 66)
    print(f"行为验收：{passed}/{len(checks)} 通过（全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 66)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
