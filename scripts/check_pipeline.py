"""4.5 Ingestion Pipeline 十一项验收脚本。

默认全部使用 Mock，不访问网络。
加 --real 才用真实采集文件做离线 Pipeline 验证。

用法：
    python scripts/check_pipeline.py           # Mock 验收，不联网
    python scripts/check_pipeline.py --real    # 额外验证真实数据

退出码：
    0  通过
    1  有项目未通过
"""

import base64
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.parser import ParseProfile, load_raw  # noqa: E402
from powercopy.data.pipeline import (  # noqa: E402
    IngestionPipeline,
    PipelineError,
    discover,
    save_fetch,
)
from powercopy.data.versioning import VersionedPolicyStore  # noqa: E402


URL = "https://example.invalid/policy/1"
KEY = "demo/detail"
PROFILE = {KEY: dict(title_selector="h1", body_selector="article")}
BASE_TIME = datetime(2026, 11, 1, tzinfo=timezone.utc)


@dataclass
class FakeResponse:
    source_key: str = "demo"
    target_key: str = "detail"
    source_url: str = URL
    content_kind: str = "policy"
    status_code: int = 200
    content_type: str = "text/html"
    charset: str = "utf-8"
    retrieved_at: datetime = BASE_TIME
    duration_ms: float = 1.0
    body: bytes = b"<h1>Policy</h1><article>Article A</article>"


class FakeError(Exception):
    def __init__(self, code, status=None):
        self.code = code
        self.status_code = status
        super().__init__(code)


class Env:
    def __init__(self, folder, profiles=None):
        self.store = VersionedPolicyStore(Path(folder) / "test.db")
        self.registry = Mock()
        self.registry.resolve.return_value = (
            None,
            SimpleNamespace(url=URL, content_kind="policy"),
        )
        self.fetcher = Mock()
        self.fetcher.fetch.return_value = FakeResponse()
        self.sleeps = []
        self.ticks = [0.0]

        def wait(seconds):
            self.sleeps.append(seconds)
            self.ticks[0] += seconds

        self.pipeline = IngestionPipeline(
            self.store,
            profiles or PROFILE,
            raw_dir=Path(folder) / "raw",
            run_dir=Path(folder) / "runs",
            registry=self.registry,
            fetcher=self.fetcher,
            approvals={KEY: dict(url=URL, approved=True, evidence="test only")},
            clock=lambda: self.ticks[0],
            sleeper=Mock(side_effect=wait),
        )
        self.sleeper = self.pipeline.sleeper


def with_env(action):
    with tempfile.TemporaryDirectory() as folder:
        env = Env(folder)
        return action(env)


# ---------------------------------------------------------------- 1 完整串联
def check_full_chain() -> bool:
    print("\n[1/11] 完整 Pipeline 串联 Fetcher、原始存储、Parser、Store")

    def run(env):
        pipeline = env.pipeline
        report = pipeline.live([("demo", "detail")])
        item = report["items"][0]
        counts = env.store.counts()
        raw_path = Path(item["raw_path"])

        return {
            "状态成功": report["status"] == "success",
            "发起 1 次请求": report["requests"] == 1,
            "fetcher 调用 1 次": env.fetcher.fetch.call_count == 1,
            "stage=process": item["stage"] == "process",
            "action=created": item["result"]["action"] == "created",
            "raw 文件已落盘": raw_path.is_file(),
            "入库 1 份文档": counts == dict(
                policies=1, versions=1, fetches=1, documents=1
            ),
        }

    checks = with_env(run)
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 2 真实数据
def check_real_offline() -> bool:
    print("\n[2/11] 两份真实政策离线入库与重复重放（--real）")

    raw_dir = ROOT / "storage" / "raw"
    profiles_path = ROOT / "config" / "parser_profiles.json"
    ndrc = raw_dir / "722b8057489c4ac2a54461d62dad117a.json"
    nea = raw_dir / "f5d31d6af4ab4e7fa76571bbe09bd53f.json"

    if not ndrc.is_file() or not nea.is_file() or not profiles_path.is_file():
        print("  [ ] 缺少真实采集文件或配置，跳过")
        return False

    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as folder:
        store = VersionedPolicyStore(Path(folder) / "v.db")
        pipe = IngestionPipeline(
            store, profiles, raw_dir=Path(folder) / "raw", run_dir=Path(folder) / "runs"
        )
        first = pipe.replay([ndrc, nea])
        second = pipe.replay([ndrc, nea])
        counts = store.counts()

    checks = {
        "首次均 created": [i["result"]["action"] for i in first["items"]]
        == ["created", "created"],
        "重放均 replayed": [i["result"]["action"] for i in second["items"]]
        == ["replayed", "replayed"],
        "离线零请求": first["requests"] == 0 and second["requests"] == 0,
        "计数 2/2/2/2": counts
        == dict(policies=2, versions=2, fetches=2, documents=2),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 3 四种动作
def check_four_actions() -> bool:
    print("\n[3/11] created、updated、unchanged、replayed 四种结果")

    def run(env):
        pipeline = env.pipeline
        first = pipeline.live([("demo", "detail")])
        created = first["items"][0]["result"]["action"]

        # 相同 fetch_id 重放
        replayed = pipeline.replay([first["items"][0]["raw_path"]])["items"][0][
            "result"
        ]["action"]

        # 同内容、新 fetch_id、更晚时间 → unchanged
        raw = load_raw(first["items"][0]["raw_path"])
        body = raw.body_bytes()
        later = raw.model_copy(
            update={
                "fetch_id": "unchanged-fetch",
                "retrieved_at": raw.retrieved_at + timedelta(hours=1),
            }
        )
        path = Path(env.pipeline.raw_dir) / "unchanged.json"
        Path(env.pipeline.raw_dir).mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(later.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        unchanged = pipeline.replay([path])["items"][0]["result"]["action"]

        # 改正文、更晚时间 → updated
        html = body.decode("utf-8").replace("Article A", "Article B")
        changed_body = html.encode("utf-8")
        changed = raw.model_copy(
            update={
                "fetch_id": "updated-fetch",
                "retrieved_at": raw.retrieved_at + timedelta(hours=2),
                "body_size": len(changed_body),
                "body_base64": base64.b64encode(changed_body).decode(),
            }
        )
        path2 = Path(env.pipeline.raw_dir) / "updated.json"
        path2.write_text(
            json.dumps(changed.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        updated = pipeline.replay([path2])["items"][0]["result"]["action"]

        return {
            "created": created == "created",
            "replayed": replayed == "replayed",
            "unchanged": unchanged == "unchanged",
            "updated": updated == "updated",
        }

    checks = with_env(run)
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 4 保存优先
def check_save_before_parse() -> bool:
    print("\n[4/11] 原始文件优先保存，解析失败后可离线恢复")

    def run(env):
        pipeline = env.pipeline
        first = pipeline.live([("demo", "detail")])
        raw_path = Path(first["items"][0]["raw_path"])

        # 用错误 profile 重新解析同一个 raw
        broken_profiles = {KEY: dict(title_selector="h1", body_selector="#missing")}
        broken = IngestionPipeline(
            env.store,
            broken_profiles,
            raw_dir=env.pipeline.raw_dir,
            run_dir=env.pipeline.run_dir,
        )
        failed_report = broken.replay([raw_path])
        failed = failed_report["items"][0]

        # 修复配置后恢复
        fixed = IngestionPipeline(
            env.store,
            PROFILE,
            raw_dir=env.pipeline.raw_dir,
            run_dir=env.pipeline.run_dir,
        )
        recovered = fixed.replay([raw_path])
        recovered_item = recovered["items"][0]

        return {
            "解析失败被标记": failed["status"] == "failed",
            "失败阶段可见": failed.get("stage") == "process",
            "raw_path 仍保留": failed["raw_path"] == str(raw_path),
            "raw 文件未被删除": raw_path.is_file(),
            "修复后恢复成功": recovered_item["status"] == "success",
            "恢复为 replayed": recovered_item["result"]["action"] == "replayed",
            "恢复无网络请求": recovered["requests"] == 0,
        }

    checks = with_env(run)
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 5 重试退避
def check_retry_backoff() -> bool:
    print("\n[5/11] 有限重试、退避与请求间隔")

    def run(env):
        pipeline = env.pipeline
        env.fetcher.fetch.side_effect = [
            FakeError("http_503", 503),
            FakeResponse(),
        ]
        report = pipeline.live([("demo", "detail")])
        item = report["items"][0]

        return {
            "两次请求": report["requests"] == 2,
            "最终成功": report["status"] == "success",
            "首次失败": item["attempts"][0]["status"] == "failed",
            "二次成功": item["attempts"][1]["status"] == "success",
            "退避 2 秒": 2 in env.sleeps,
            "记录 http 状态": item["attempts"][0].get("http_status") == 503,
        }

    checks = with_env(run)
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")

    # 请求间隔
    def run_interval(env):
        pipeline = env.pipeline
        report = pipeline.live([("demo", "detail"), ("demo", "detail")])
        has_interval = any(s == 2 for s in env.sleeps)
        return {
            "两次目标两次请求": report["requests"] == 2,
            "间隔 >= 2 秒": has_interval,
        }

    more = with_env(run_interval)
    for name, ok in more.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values()) and all(more.values())


# ---------------------------------------------------------------- 6 预算与尝试
def check_budget() -> bool:
    print("\n[6/11] 全局请求预算及单目标尝试限制")

    def run_budget(env):
        env.pipeline.max_requests = 3
        report = env.pipeline.live([("demo", "detail")] * 5)
        return {
            "请求不超过预算": report["requests"] <= 3,
            "后续目标被拒绝": any(
                i.get("error_code") == "REQUEST_BUDGET_EXHAUSTED"
                for i in report["items"]
            ),
        }

    def run_attempts(env):
        env.fetcher.fetch.side_effect = [FakeError("http_503", 503)] * 10
        report = env.pipeline.live([("demo", "detail")])
        return {
            "单目标最多 2 次": report["requests"] == 2,
            "最终失败": report["status"] == "failed",
        }

    checks = with_env(run_budget)
    checks2 = with_env(run_attempts)
    for name, ok in list(checks.items()) + list(checks2.items()):
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values()) and all(checks2.values())


# ---------------------------------------------------------------- 7 暂停与批准
def check_pause_and_approval() -> bool:
    print("\n[7/11] 403/429 暂停、未批准目标不发送请求")

    results = {}
    for status in (403, 429):
        def run(env, status=status):
            env.fetcher.fetch.side_effect = FakeError(f"http_{status}", status)
            report = env.pipeline.live([("demo", "detail")] * 3)
            return {
                f"{status} 只请求 1 次": report["requests"] == 1,
                f"{status} 后续目标 RUN_PAUSED": report["items"][1].get(
                    "error_code"
                )
                == "RUN_PAUSED",
                f"{status} 不自动重试": len(report["items"][0]["attempts"]) == 1,
            }

        results.update(with_env(run))

    def run_approval(env):
        env.pipeline.approvals = {}
        report = env.pipeline.live([("demo", "detail")])
        return {
            "无批准不发请求": report["requests"] == 0,
            "错误码正确": report["items"][0].get("error_code")
            == "NO_OPERATOR_APPROVAL",
            "fetcher 未被调用": env.fetcher.fetch.call_count == 0,
        }

    results.update(with_env(run_approval))

    for name, ok in results.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(results.values())


# ---------------------------------------------------------------- 8 真实列表页
def check_index_selector() -> bool:
    print("\n[8/11] 真实列表页选择器校准及候选发现")

    selectors_path = ROOT / "config" / "index_selectors.json"
    index_raw = ROOT / "storage" / "raw" / "fd45e1c38564451b8e1d382360ade61c.json"

    if not selectors_path.is_file() or not index_raw.is_file():
        print("  [ ] 缺少列表页原始文件或选择器配置，跳过")
        return False

    selectors = json.loads(selectors_path.read_text(encoding="utf-8"))
    selector = selectors["nea_east/policy_index"]
    raw = load_raw(index_raw)
    candidates = discover(raw, selector, limit=20)

    checks = {
        "选择器非占位符": "REPLACE_WITH" not in selector,
        "发现 15 条候选": len(candidates) == 15,
        "全部 pending_review": all(
            c["status"] == "pending_review" for c in candidates
        ),
        "全部 https": all(c["url"].startswith("https://") for c in candidates),
        "全部同域名": all("hdj.nea.gov.cn" in c["url"] for c in candidates),
        "URL 无重复": len({c["url"] for c in candidates}) == len(candidates),
        "标题非空": all(c["title"].strip() for c in candidates),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")

    print("\n  部分候选标题：")
    for c in candidates[:3]:
        print(f"    - {c['title'][:56]}")

    return all(checks.values())


# ---------------------------------------------------------------- 9 过滤与隔离
def check_filter_and_isolation() -> bool:
    print("\n[9/11] 候选过滤、去重、人工审核边界与新闻隔离")

    def run(env):
        pipeline = env.pipeline
        # 列表页候选不入库
        index_html = (
            '<div><a href="/a">A</a><a href="https://evil.invalid/b">B</a>'
            '<a href="/a">A dup</a></div>'
        ).encode()
        r = FakeResponse(
            target_key="detail",
            content_kind="index",
            body=index_html,
            source_url=URL,
        )
        env.registry.resolve.return_value = (
            None,
            SimpleNamespace(url=URL, content_kind="index"),
        )
        env.fetcher.fetch.return_value = r
        report = pipeline.live([("demo", "detail")], {KEY: "div a"})
        item = report["items"][0]
        candidates = item["result"]["candidates"]
        not_persisted = env.store.counts()["documents"] == 0

        # 新闻仅归档
        env.registry.resolve.return_value = (
            None,
            SimpleNamespace(url=URL, content_kind="market_news"),
        )
        env.fetcher.fetch.return_value = FakeResponse(content_kind="market_news")
        news_report = pipeline.live([("demo", "detail")])
        news_item = news_report["items"][0]

        return {
            "域外链接被过滤": len(candidates) == 1,
            "重复链接已去重": len({c["url"] for c in candidates}) == len(candidates),
            "候选不自动注册": env.registry.register.call_count == 0,
            "候选不入库": not_persisted,
            "新闻 action=archived_only": news_item["result"]["action"]
            == "archived_only",
        }

    checks = with_env(run)
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


# ---------------------------------------------------------------- 10 报告与隔离
def check_report_and_isolation() -> bool:
    print("\n[10/11] 运行报告、失败阶段与失败隔离")

    def run(env):
        pipeline = env.pipeline
        report = pipeline.live([("demo", "detail")])
        saved = json.loads(
            (pipeline.run_dir / f"{report['run_id']}.json").read_text(
                encoding="utf-8"
            )
        )

        # 失败隔离
        broken = {KEY: dict(title_selector="h1", body_selector="#missing")}
        first = pipeline.live([("demo", "detail")])
        raw_path = first["items"][0]["raw_path"]

        broken_pipe = IngestionPipeline(
            env.store,
            broken,
            raw_dir=pipeline.raw_dir,
            run_dir=pipeline.run_dir,
        )
        report2 = broken_pipe.replay([raw_path])
        failed_item = report2["items"][0]

        return {
            "报告已落盘": saved == report,
            "报告含 requests": saved["requests"] == 1,
            "报告含 attempts": len(saved["items"][0]["attempts"]) == 1,
            "失败阶段可见": failed_item.get("stage") == "process",
            "失败有 error_code": bool(failed_item.get("error_code")),
            "报告状态 partial/failed": report2["status"] in {"failed", "partial"},
        }

    checks = with_env(run)
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def main() -> int:
    real = "--real" in sys.argv

    print("=" * 66)
    print("PowerCopy V2 · 4.5 Ingestion Pipeline 验收")
    print("默认使用 Mock" + ("；--real 验证真实数据" if real else ""))
    print("=" * 66)

    checks = [
        ("完整 Pipeline 串联", check_full_chain()),
        ("created/updated/unchanged/replayed", check_four_actions()),
        ("原始文件优先保存与恢复", check_save_before_parse()),
        ("有限重试、退避与间隔", check_retry_backoff()),
        ("请求预算与尝试限制", check_budget()),
        ("403/429 暂停与批准机制", check_pause_and_approval()),
        ("候选过滤、去重与隔离", check_filter_and_isolation()),
        ("运行报告与失败隔离", check_report_and_isolation()),
    ]

    if real:
        checks.insert(1, ("真实政策离线入库与重放", check_real_offline()))
        checks.append(("真实列表页选择器", check_index_selector()))

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 66)
    print(f"行为验收：{passed}/{len(checks)} 通过（全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 66)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
