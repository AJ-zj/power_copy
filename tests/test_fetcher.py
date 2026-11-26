"""Source Registry 与 Fetcher 测试（全 Mock，不访问网络）。"""

import base64
import json
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import MagicMock, Mock

import pytest
from urllib.error import HTTPError, URLError
from urllib.request import Request

from powercopy.data.fetcher import (
    FetchError,
    FetchResult,
    HttpFetcher,
    NoRedirect,
)
from powercopy.data.sources import (
    DataSource,
    FetchTarget,
    SourceConfigError,
    SourceRegistry,
    build_source_registry,
)


@pytest.fixture
def registry():
    return build_source_registry()


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


def make_fetcher(registry, opener, **kwargs):
    return HttpFetcher(registry, opener=opener, **kwargs)


# ============================================================ 1. Registry
class TestSourceRegistry:
    def test_build_default_registry(self, registry):
        assert len(registry.list_sources()) == 3
        assert "ndrc" in registry
        assert "nea" in registry
        assert "nea_east" in registry

    def test_resolve_returns_source_and_target(self, registry):
        source, target = registry.resolve("ndrc", "market_rules")
        assert source.source_key == "ndrc"
        assert target.content_kind == "policy"
        assert target.url.startswith("https://zfxxgk.ndrc.gov.cn/")

    def test_all_three_sources_have_expected_targets(self, registry):
        expected = {
            "ndrc": {"market_rules", "policy_index"},
            "nea": {"registration_rules"},
            "nea_east": {"anhui_market_news", "policy_index"},
        }
        for key, targets in expected.items():
            source = registry.get(key)
            assert {t.target_key for t in source.targets} == targets

    def test_duplicate_source_key_rejected(self, registry):
        with pytest.raises(SourceConfigError, match="duplicate source_key"):
            registry.register(
                DataSource(
                    source_key="ndrc",
                    name="重复",
                    allowed_host="zfxxgk.ndrc.gov.cn",
                    targets=(FetchTarget("x", "https://zfxxgk.ndrc.gov.cn/a", "policy"),),
                )
            )

    def test_duplicate_target_key_rejected(self):
        reg = SourceRegistry()
        with pytest.raises(SourceConfigError, match="duplicate target_key"):
            reg.register(
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

    def test_unknown_source_raises_keyerror(self, registry):
        with pytest.raises(KeyError, match="未知数据源"):
            registry.get("ghost")

    def test_unknown_target_raises_keyerror(self, registry):
        with pytest.raises(KeyError, match="没有采集目标"):
            registry.resolve("ndrc", "ghost_target")

    def test_empty_targets_rejected(self):
        reg = SourceRegistry()
        with pytest.raises(SourceConfigError, match="至少需要一个采集目标"):
            reg.register(
                DataSource(
                    source_key="demo",
                    name="演示",
                    allowed_host="example.invalid",
                    targets=(),
                )
            )

    def test_blank_source_key_rejected(self):
        reg = SourceRegistry()
        with pytest.raises(SourceConfigError):
            reg.register(
                DataSource(
                    source_key="   ",
                    name="演示",
                    allowed_host="example.invalid",
                    targets=(
                        FetchTarget("a", "https://example.invalid/a", "policy"),
                    ),
                )
            )

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.invalid/a",
            "ftp://example.invalid/a",
            "file:///etc/passwd",
        ],
    )
    def test_non_https_url_rejected(self, url):
        reg = SourceRegistry()
        with pytest.raises(SourceConfigError):
            reg.register(
                DataSource(
                    source_key="demo",
                    name="演示",
                    allowed_host="example.invalid",
                    targets=(FetchTarget("a", url, "policy"),),
                )
            )

    def test_host_mismatch_rejected(self):
        reg = SourceRegistry()
        with pytest.raises(SourceConfigError, match="不一致"):
            reg.register(
                DataSource(
                    source_key="demo",
                    name="演示",
                    allowed_host="example.invalid",
                    targets=(
                        FetchTarget("a", "https://other.invalid/a", "policy"),
                    ),
                )
            )

    def test_listsources_returns_copy(self, registry):
        names = registry.list_sources()
        names.append("injected")
        assert "injected" not in registry.list_sources()


# ============================================================ 2. 未知/禁用
class TestUnknownAndDisabled:
    def test_unknown_source_no_network(self, registry):
        opener, _ = make_response("https://example.invalid/")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ghost", "market_rules")

        assert exc.value.code == "unknown_source_or_target"
        opener.open.assert_not_called()

    def test_unknown_target_no_network(self, registry):
        opener, _ = make_response("https://example.invalid/")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "ghost_target")

        assert exc.value.code == "unknown_source_or_target"
        opener.open.assert_not_called()

    def test_disabled_source_no_network(self):
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
        opener, _ = make_response("https://example.invalid/a")
        fetcher = make_fetcher(reg, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("off", "a")

        assert exc.value.code == "source_disabled"
        opener.open.assert_not_called()


# ============================================================ 3. URL/域名
class TestUrlAndHost:
    def test_host_mismatch_after_redirect_target(self, registry):
        """响应最终 URL 落到其他域名时必须拒绝。"""
        opener, _ = make_response("https://evil.invalid/policy")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "host_not_allowed"

    def test_subdomain_confusion_rejected(self, registry):
        """zfxxgk.ndrc.gov.cn.evil.com 这类伪装必须拒绝。"""
        opener, _ = make_response("https://zfxxgk.ndrc.gov.cn.evil.invalid/a")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "host_not_allowed"

    def test_localhost_rejected(self, registry):
        opener, _ = make_response("https://127.0.0.1/a")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "host_not_allowed"

    def test_invalid_scheme_after_request(self, registry):
        opener, _ = make_response("http://zfxxgk.ndrc.gov.cn/a")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "invalid_scheme"


# ============================================================ 4. 正常响应
class TestSuccessfulFetch:
    def test_returns_fetch_result(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, body=b"<html>ok</html>")
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")

        assert isinstance(result, FetchResult)
        assert result.source_key == "ndrc"
        assert result.target_key == "market_rules"
        assert result.content_kind == "policy"
        assert result.status_code == 200
        assert result.body == b"<html>ok</html>"
        assert result.body_size == len(b"<html>ok</html>")

    def test_metadata_recorded(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url)
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")

        assert result.content_type == "text/html"
        assert result.charset == "utf-8"
        assert isinstance(result.duration_ms, float)
        assert result.duration_ms >= 0
        assert isinstance(result.retrieved_at, datetime)
        assert result.retrieved_at.tzinfo is not None

    def test_content_type_without_charset(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, content_type="text/html")
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")
        assert result.content_type == "text/html"
        assert result.charset is None

    def test_missing_content_type(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, content_type=None)
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")
        assert result.content_type is None
        assert result.charset is None

    def test_gbk_charset_recorded(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, content_type='text/html; charset="GBK"')
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")
        assert result.charset == "gbk"


# ============================================================ 5. 错误分类
class TestErrorClassification:
    @pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
    def test_http_error_classified(self, registry, status):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://zfxxgk.ndrc.gov.cn/x", status, "err", Message(), None
        )
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == f"http_{status}"
        assert exc.value.status_code == status

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_redirect_rejected(self, registry, status):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://zfxxgk.ndrc.gov.cn/x", status, "redirect", Message(), None
        )
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "redirect_not_allowed"

    def test_network_error(self, registry):
        opener = Mock()
        opener.open.side_effect = URLError(OSError("connection refused"))
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "network_error"

    def test_timeout_from_urlerror(self, registry):
        opener = Mock()
        opener.open.side_effect = URLError(TimeoutError("timed out"))
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "timeout"

    def test_timeout_direct(self, registry):
        opener = Mock()
        opener.open.side_effect = TimeoutError("timed out")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "timeout"


# ============================================================ 6. 大小/空响应
class TestSizeLimits:
    def test_response_too_large(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, body=b"x" * 101)
        fetcher = make_fetcher(registry, opener, max_bytes=100)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "response_too_large"

    def test_exactly_max_bytes_allowed(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, body=b"x" * 100)
        fetcher = make_fetcher(registry, opener, max_bytes=100)

        result = fetcher.fetch("ndrc", "market_rules")
        assert result.body_size == 100

    def test_reads_max_bytes_plus_one(self, registry):
        """必须多读 1 字节才能识别越界。"""
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, response = make_response(url)
        fetcher = make_fetcher(registry, opener, max_bytes=1024)

        fetcher.fetch("ndrc", "market_rules")
        response.read.assert_called_once_with(1025)

    def test_empty_response(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, body=b"")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "empty_response"

    @pytest.mark.parametrize("kwarg,value", [("timeout", 0), ("max_bytes", 0),
                                            ("timeout", -1), ("max_bytes", -5)])
    def test_invalid_constructor_args(self, registry, kwarg, value):
        with pytest.raises(ValueError):
            HttpFetcher(registry, **{kwarg: value})


# ============================================================ 7. 重定向
class TestNoRedirect:
    def test_no_redirect_handler_returns_none(self):
        handler = NoRedirect()
        result = handler.redirect_request(
            Request("https://a.invalid"),
            None,
            302,
            "Found",
            Message(),
            "https://b.invalid",
        )
        assert result is None

    def test_host_check_after_opener_returns_other_host(self, registry):
        """即使 mock opener 返回了别的域名，也要在读取前拒绝。"""
        opener, _ = make_response("https://other.invalid/a", body=b"x")
        fetcher = make_fetcher(registry, opener)

        with pytest.raises(FetchError) as exc:
            fetcher.fetch("ndrc", "market_rules")

        assert exc.value.code == "host_not_allowed"


# ============================================================ 8. 原始字节
class TestRawBytes:
    def test_body_is_bytes_not_str(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(url, body="中文政策".encode("gbk"))
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")

        assert isinstance(result.body, bytes)
        assert result.body == "中文政策".encode("gbk")
        # 未做任何解码
        assert result.body.decode("gbk") == "中文政策"

    def test_non_utf8_body_preserved(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        raw = b"\xd6\xd0\xce\xc4"  # GBK "中文"
        opener, _ = make_response(url, body=raw)
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")
        assert result.body == raw

    def test_charset_hint_kept_separately(self, registry):
        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        opener, _ = make_response(
            url, body="中文".encode("gb18030"),
            content_type="text/html; charset=gb18030",
        )
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")
        assert result.charset == "gb18030"
        assert result.body.decode("gb18030") == "中文"


# ============================================================ 9. Base64 往返
class TestBase64RoundTrip:
    def test_base64_roundtrip(self):
        original = "<html>中文政策</html>".encode("utf-8")
        encoded = base64.b64encode(original).decode("ascii")
        restored = base64.b64decode(encoded.encode("ascii"))

        assert restored == original
        assert restored.decode("utf-8") == "<html>中文政策</html>"

    def test_base64_in_json(self):
        body = bytes(range(256))
        encoded = base64.b64encode(body).decode("ascii")
        payload = json.dumps({"body_base64": encoded}, ensure_ascii=False)

        loaded = json.loads(payload)
        assert base64.b64decode(loaded["body_base64"].encode("ascii")) == body

    def test_fetch_result_serializable_shape(self, registry):
        """模拟脚本的落盘结构，验证元数据 + Base64 能完整还原 body。"""
        from dataclasses import asdict

        url = "https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379"
        raw = "<html>政策正文</html>".encode("utf-8")
        opener, _ = make_response(url, body=raw)
        fetcher = make_fetcher(registry, opener)

        result = fetcher.fetch("ndrc", "market_rules")

        data = asdict(result)
        body = data.pop("body")
        data["body_base64"] = base64.b64encode(body).decode("ascii")
        data["body_size"] = len(body)
        data["retrieved_at"] = result.retrieved_at.isoformat()

        text = json.dumps(data, ensure_ascii=False, indent=2)
        loaded = json.loads(text)

        assert loaded["body_size"] == len(raw)
        assert base64.b64decode(loaded["body_base64"].encode("ascii")) == raw
        assert loaded["source_key"] == "ndrc"
        assert loaded["status_code"] == 200
        assert "T" in loaded["retrieved_at"]


# ============================================================ 附加
class TestDefaults:
    def test_default_timeout_and_max_bytes(self, registry):
        fetcher = HttpFetcher(registry)
        assert fetcher.timeout == 10.0
        assert fetcher.max_bytes == 2 * 1024 * 1024

    def test_custom_values(self, registry):
        fetcher = HttpFetcher(registry, timeout=5.0, max_bytes=1024)
        assert fetcher.timeout == 5.0
        assert fetcher.max_bytes == 1024
