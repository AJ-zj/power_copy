"""Source Registry & Fetcher 十一项验收脚本。

默认全部使用 Mock，不访问网络。
加 --live 才执行真实官方网页采集（会发起真实 HTTPS 请求）。

用法：
    python scripts/check_fetcher.py           # Mock 验收，不联网
    python scripts/check_fetcher.py --live    # 额外做真实采集

验收十一项：
    1. 数据源注册、查询与重复注册检查
    2. 未知或禁用数据源不发送网络请求
    3. 非法 URL 与域名检查
    4. HTTP 请求、响应元数据正确记录
    5. HTTP 错误、网络异常与超时正确分类
    6. 响应大小限制与空响应检查
    7. 禁止自动重定向
    8. 原始字节及编码信息完整保留
    9. 原始 JSON 文件保存与 Base64 往返
    10. 经过授权的真实官方网页采集（--live）
    11. 全量回归（由 pytest 保证）

退出码：
    0  通过
    1  有项目未通过
"""

import base64
import json
import sys
import tempfile
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.fetcher import (  # noqa: E402
    FetchError,
    HttpFetcher,
    NoRedirect,
)
from powercopy.data.sources import (  # noqa: E402
    DataSource,
    FetchTarget,
    SourceConfigError,
    SourceRegistry,
    build_source_registry,
)


NDRC_URL = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"


def make_response(
    url,
    body=b"<html>policy</html>",
    content_type="text/html; charset=utf-8",
    status=200,
):
    response = MagicMock()
    headers = Message()
    if content_type is not None:
        headers["Content-Type"] = content_type
    response.headers = headers
    response.status = status
    response.geturl.return_value = url
    response.read.return_value = body
    response.__enter__.return_value = response

    opener = Mock()
    opener.open.return_value = response
    return opener, response


def check_registry() -> bool:
    print("\n[1/11] 数据源注册、查询与重复注册检查")

    registry = build_source_registry()
    source, target = registry.resolve("ndrc", "market_rules")

    checks = {
        "注册 3 个数据源": len(registry.list_sources()) == 3,
        "source_key 正确": source.source_key == "ndrc",
        "content_kind 正确": target.content_kind == "policy",
        "URL 是 https": target.url.startswith("https://"),
    }

    # 重复 source_key
    try:
        registry.register(
            DataSource(
                source_key="ndrc",
                name="重复",
                allowed_host="zfxxgk.ndrc.gov.cn",
                targets=(FetchTarget("x", NDRC_URL, "policy"),),
            )
        )
        checks["重复 source_key 被拒绝"] = False
    except SourceConfigError:
        checks["重复 source_key 被拒绝"] = True

    # 重复 target_key
    reg2 = SourceRegistry()
    try:
        reg2.register(
            DataSource(
                source_key="demo",
                name="演示",
                allowed_host="example.invalid",
                targets=(
                    FetchTarget("dup", "https://example.invalid/a", "policy"),
                    FetchTarget("dup", "https://example.invalid/b", "policy"),
                ),
            )
        )
        checks["重复 target_key 被拒绝"] = False
    except SourceConfigError:
        checks["重复 target_key 被拒绝"] = True

    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_unknown_and_disabled() -> bool:
    print("\n[2/11] 未知或禁用数据源不发送网络请求")

    registry = build_source_registry()

    opener, _ = make_response("https://example.invalid/")
    fetcher = HttpFetcher(registry, opener=opener)

    unknown_ok = False
    try:
        fetcher.fetch("ghost", "market_rules")
    except FetchError as error:
        unknown_ok = error.code == "unknown_source_or_target"
    no_call = opener.open.call_count == 0

    opener2, _ = make_response("https://example.invalid/")
    fetcher2 = HttpFetcher(registry, opener=opener2)
    target_ok = False
    try:
        fetcher2.fetch("ndrc", "ghost_target")
    except FetchError as error:
        target_ok = error.code == "unknown_source_or_target"
    no_call2 = opener2.open.call_count == 0

    # 禁用数据源
    reg = SourceRegistry()
    reg.register(
        DataSource(
            source_key="off",
            name="停用",
            allowed_host="example.invalid",
            targets=(FetchTarget("a", "https://example.invalid/a", "policy"),),
            enabled=False,
        )
    )
    opener3, _ = make_response("https://example.invalid/a")
    fetcher3 = HttpFetcher(reg, opener=opener3)
    disabled_ok = False
    try:
        fetcher3.fetch("off", "a")
    except FetchError as error:
        disabled_ok = error.code == "source_disabled"
    no_call3 = opener3.open.call_count == 0

    checks = {
        "未知数据源返回 unknown_source_or_target": unknown_ok,
        "未知数据源未发请求": no_call,
        "未知 target 返回错误": target_ok,
        "未知 target 未发请求": no_call2,
        "禁用数据源返回 source_disabled": disabled_ok,
        "禁用数据源未发请求": no_call3,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_url_and_host() -> bool:
    print("\n[3/11] 非法 URL 与域名检查")

    registry = build_source_registry()

    cases = {
        "子域名混淆": "https://zfxxgk.ndrc.gov.cn.evil.invalid/a",
        "localhost": "https://127.0.0.1/a",
        "其他域名": "https://evil.invalid/a",
    }
    all_ok = True
    for label, final_url in cases.items():
        opener, _ = make_response(final_url)
        fetcher = HttpFetcher(registry, opener=opener)
        try:
            fetcher.fetch("ndrc", "market_rules")
            print(f"  [ ] {label} 未被拒绝")
            all_ok = False
        except FetchError as error:
            ok = error.code == "host_not_allowed"
            all_ok = all_ok and ok
            print(f"  [{'x' if ok else ' '}] {label} 被拒绝（{error.code}）")

    # http 协议
    opener, _ = make_response("http://zfxxgk.ndrc.gov.cn/a")
    fetcher = HttpFetcher(registry, opener=opener)
    try:
        fetcher.fetch("ndrc", "market_rules")
        scheme_ok = False
    except FetchError as error:
        scheme_ok = error.code == "invalid_scheme"
    all_ok = all_ok and scheme_ok
    print(f"  [{'x' if scheme_ok else ' '}] http 协议被拒绝")

    # 注册期就拒绝域名不一致
    reg = SourceRegistry()
    try:
        reg.register(
            DataSource(
                source_key="demo",
                name="演示",
                allowed_host="example.invalid",
                targets=(FetchTarget("a", "https://other.invalid/a", "policy"),),
            )
        )
        reg_ok = False
    except SourceConfigError:
        reg_ok = True
    all_ok = all_ok and reg_ok
    print(f"  [{'x' if reg_ok else ' '}] 注册期拒绝域名不一致")

    return all_ok


def check_metadata() -> bool:
    print("\n[4/11] HTTP 请求、响应元数据正确记录")

    registry = build_source_registry()
    opener, response = make_response(
        NDRC_URL, body=b"<html>ok</html>",
        content_type='text/html; charset="UTF-8"',
    )
    fetcher = HttpFetcher(registry, opener=opener)
    result = fetcher.fetch("ndrc", "market_rules")

    checks = {
        "source_key 正确": result.source_key == "ndrc",
        "target_key 正确": result.target_key == "market_rules",
        "url 正确": result.source_url == NDRC_URL,
        "status_code=200": result.status_code == 200,
        "content_type=text/html": result.content_type == "text/html",
        "charset 小写归一": result.charset == "utf-8",
        "body 正确": result.body == b"<html>ok</html>",
        "body_size 正确": result.body_size == len(b"<html>ok</html>"),
        "duration_ms 是浮点": isinstance(result.duration_ms, float),
        "retrieved_at 带时区": result.retrieved_at.tzinfo is not None,
        "只读 max_bytes+1": response.read.call_args[0][0] == fetcher.max_bytes + 1,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_error_classification() -> bool:
    print("\n[5/11] HTTP 错误、网络异常与超时分类")

    registry = build_source_registry()
    all_ok = True

    for status in (400, 403, 404, 429, 500, 503):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            NDRC_URL, status, "err", Message(), None
        )
        fetcher = HttpFetcher(registry, opener=opener)
        try:
            fetcher.fetch("ndrc", "market_rules")
            ok = False
        except FetchError as error:
            ok = error.code == f"http_{status}" and error.status_code == status
        all_ok = all_ok and ok
        print(f"  [{'x' if ok else ' '}] HTTP {status} -> http_{status}")

    # 网络错误
    opener = Mock()
    opener.open.side_effect = URLError(OSError("connection refused"))
    fetcher = HttpFetcher(registry, opener=opener)
    try:
        fetcher.fetch("ndrc", "market_rules")
        net_ok = False
    except FetchError as error:
        net_ok = error.code == "network_error"
    all_ok = all_ok and net_ok
    print(f"  [{'x' if net_ok else ' '}] 网络错误 -> network_error")

    # 超时（URLError 包装）
    opener = Mock()
    opener.open.side_effect = URLError(TimeoutError("timed out"))
    fetcher = HttpFetcher(registry, opener=opener)
    try:
        fetcher.fetch("ndrc", "market_rules")
        t1 = False
    except FetchError as error:
        t1 = error.code == "timeout"
    all_ok = all_ok and t1
    print(f"  [{'x' if t1 else ' '}] URLError(timeout) -> timeout")

    # 超时（直接抛出）
    opener = Mock()
    opener.open.side_effect = TimeoutError("timed out")
    fetcher = HttpFetcher(registry, opener=opener)
    try:
        fetcher.fetch("ndrc", "market_rules")
        t2 = False
    except FetchError as error:
        t2 = error.code == "timeout"
    all_ok = all_ok and t2
    print(f"  [{'x' if t2 else ' '}] TimeoutError -> timeout")

    return all_ok


def check_size_and_empty() -> bool:
    print("\n[6/11] 响应大小限制与空响应")

    registry = build_source_registry()

    opener, _ = make_response(NDRC_URL, body=b"x" * 101)
    fetcher = HttpFetcher(registry, opener=opener, max_bytes=100)
    try:
        fetcher.fetch("ndrc", "market_rules")
        too_large_ok = False
    except FetchError as error:
        too_large_ok = error.code == "response_too_large"

    opener2, _ = make_response(NDRC_URL, body=b"x" * 100)
    fetcher2 = HttpFetcher(registry, opener=opener2, max_bytes=100)
    exact_ok = fetcher2.fetch("ndrc", "market_rules").body_size == 100

    opener3, _ = make_response(NDRC_URL, body=b"")
    fetcher3 = HttpFetcher(registry, opener=opener3)
    try:
        fetcher3.fetch("ndrc", "market_rules")
        empty_ok = False
    except FetchError as error:
        empty_ok = error.code == "empty_response"

    checks = {
        "超限返回 response_too_large": too_large_ok,
        "恰好等于上限允许": exact_ok,
        "空响应返回 empty_response": empty_ok,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_redirect() -> bool:
    print("\n[7/11] 禁止自动重定向")

    handler = NoRedirect()
    result = handler.redirect_request(
        Request("https://a.invalid"), None, 302, "Found", Message(), "https://b.invalid"
    )
    handler_ok = result is None

    registry = build_source_registry()
    all_ok = handler_ok
    print(f"  [{'x' if handler_ok else ' '}] NoRedirect 返回 None")

    for status in (301, 302, 303, 307, 308):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            NDRC_URL, status, "redirect", Message(), None
        )
        fetcher = HttpFetcher(registry, opener=opener)
        try:
            fetcher.fetch("ndrc", "market_rules")
            ok = False
        except FetchError as error:
            ok = error.code == "redirect_not_allowed"
        all_ok = all_ok and ok
        print(f"  [{'x' if ok else ' '}] HTTP {status} -> redirect_not_allowed")

    return all_ok


def check_raw_bytes() -> bool:
    print("\n[8/11] 原始字节及编码信息完整保留")

    registry = build_source_registry()

    raw = "中文政策".encode("gbk")
    opener, _ = make_response(
        NDRC_URL, body=raw, content_type="text/html; charset=gbk"
    )
    fetcher = HttpFetcher(registry, opener=opener)
    result = fetcher.fetch("ndrc", "market_rules")

    checks = {
        "body 是 bytes": isinstance(result.body, bytes),
        "原始字节未被改动": result.body == raw,
        "charset 记录正确": result.charset == "gbk",
        "可正确解码": result.body.decode("gbk") == "中文政策",
        "content_type 独立记录": result.content_type == "text/html",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_base64_roundtrip() -> bool:
    print("\n[9/11] 原始 JSON 文件保存与 Base64 往返")

    registry = build_source_registry()
    raw = "<html>中文正文</html>".encode("utf-8")
    opener, _ = make_response(NDRC_URL, body=raw)
    fetcher = HttpFetcher(registry, opener=opener)
    result = fetcher.fetch("ndrc", "market_rules")

    from dataclasses import asdict

    with tempfile.TemporaryDirectory() as folder:
        data = asdict(result)
        body = data.pop("body")
        data["body_base64"] = base64.b64encode(body).decode("ascii")
        data["body_size"] = len(body)
        data["retrieved_at"] = result.retrieved_at.isoformat()

        path = Path(folder) / "fetch.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        loaded = json.loads(path.read_text(encoding="utf-8"))
        restored = base64.b64decode(loaded["body_base64"].encode("ascii"))

    checks = {
        "文件可写入并读回": loaded["source_key"] == "ndrc",
        "body_size 一致": loaded["body_size"] == len(raw),
        "Base64 可还原": restored == raw,
        "还原后可解码": restored.decode("utf-8") == "<html>中文正文</html>",
        "元数据完整": loaded["status_code"] == 200
        and "retrieved_at" in loaded,
        "JSON 无 bytes 残留": "body" not in loaded,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_live() -> bool:
    print("\n[10/11] 真实官方网页采集（--live）")

    registry = build_source_registry()
    fetcher = HttpFetcher(registry, timeout=15.0)

    targets = [
        ("ndrc", "market_rules"),
        ("nea", "registration_rules"),
        ("nea_east", "anhui_market_news"),
    ]

    all_ok = True
    for source_key, target_key in targets:
        try:
            result = fetcher.fetch(source_key, target_key)
        except FetchError as error:
            print(
                f"  [ ] {source_key}/{target_key} 采集失败："
                f"{error.code} HTTP={error.status_code}"
            )
            all_ok = False
            continue

        ok = (
            result.status_code == 200
            and result.body_size > 0
            and result.content_type is not None
            and result.content_type.startswith("text/html")
        )
        all_ok = all_ok and ok
        print(
            f"  [{'x' if ok else ' '}] {source_key}/{target_key} "
            f"HTTP {result.status_code} · {result.body_size} bytes · "
            f"{result.duration_ms} ms"
        )

    return all_ok


def main() -> int:
    live = "--live" in sys.argv

    print("=" * 64)
    print("PowerCopy V2 · Source Registry & Fetcher 验收")
    print("默认全程 Mock，不访问网络" + ("；--live 将真实采集" if live else ""))
    print("=" * 64)

    checks = [
        ("数据源注册、查询与重复注册", check_registry()),
        ("未知或禁用数据源不发请求", check_unknown_and_disabled()),
        ("非法 URL 与域名检查", check_url_and_host()),
        ("请求与响应元数据", check_metadata()),
        ("错误、网络异常与超时分类", check_error_classification()),
        ("响应大小与空响应", check_size_and_empty()),
        ("禁止自动重定向", check_redirect()),
        ("原始字节与编码", check_raw_bytes()),
        ("Base64 往返", check_base64_roundtrip()),
    ]

    if live:
        checks.append(("真实官方网页采集", check_live()))

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 64)
    print(
        f"行为验收：{passed}/{len(checks)} 通过"
        "（全量回归由 pytest 保证）"
    )
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 64)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
