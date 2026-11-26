"""HTTP 数据采集器（Fetcher）。

职责只有一件事：按注册中心里预先审核的地址，安全地取回原始网页字节。

它不做：
- 不解析 HTML，不提取政策正文（4.3 Parser 负责）
- 不重试、不跟随重定向（重试与限流在 4.5 统一设计）
- 不参与任何模型调用

设计要点：
1. 精确域名校验，防止配置出错或 URL 被改写后访问到其他站点；
2. 禁用环境代理，避免请求被未知代理转发；
3. 禁止自动重定向，遇到 3xx 直接报错；
4. 响应体保留 bytes，编码判断交给 Parser；
5. 限制最大读取字节数，读 max_bytes + 1 用于识别越界。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from powercopy.data.sources import FetchTarget, SourceRegistry


DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0
USER_AGENT = "PowerCopyAgent/0.2 (policy fetcher; contact: internal)"


class FetchError(Exception):
    """采集失败，code 表示失败类别。"""

    def __init__(
        self,
        code: str,
        status_code: int | None = None,
    ):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class NoRedirect(HTTPRedirectHandler):
    """禁止自动重定向：任何 3xx 都抛 HTTPError。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class FetchResult:
    """一次采集的原始结果。

    注意：body 是原始字节，未做任何解码。
    """

    source_key: str
    target_key: str
    source_url: str
    content_kind: str
    status_code: int
    content_type: str | None
    charset: str | None
    body: bytes
    body_size: int
    duration_ms: float
    retrieved_at: datetime


def _parse_content_type(header_value: str | None) -> tuple[str | None, str | None]:
    """从 Content-Type 里拆出媒体类型与字符集。"""

    if not header_value:
        return None, None

    parts = [part.strip() for part in header_value.split(";")]
    media_type = parts[0].lower() or None

    charset = None
    for part in parts[1:]:
        if "=" in part:
            key, _, value = part.partition("=")
            if key.strip().lower() == "charset":
                charset = value.strip().strip('"').lower() or None

    return media_type, charset


class HttpFetcher:
    """受控的 HTTPS 网页采集器。"""

    def __init__(
        self,
        registry: SourceRegistry,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        opener=None,
    ):
        if timeout <= 0:
            raise ValueError("timeout 必须大于零")

        if max_bytes <= 0:
            raise ValueError("max_bytes 必须大于零")

        self.registry = registry
        self.timeout = timeout
        self.max_bytes = max_bytes

        # 依赖注入：测试可以传入 mock opener，
        # 生产环境构建「禁代理 + 禁重定向」的真实 opener。
        self._opener = opener if opener is not None else build_opener(
            ProxyHandler({}),
            NoRedirect(),
        )

    def fetch(self, source_key: str, target_key: str) -> FetchResult:
        """按 source_key + target_key 采集一次。"""

        source, target = self._resolve(source_key, target_key)

        self._check_url(source.allowed_host, target.url)

        started = perf_counter()

        request = Request(
            target.url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
            method="GET",
        )

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status_code = response.status
                final_url = response.geturl()
                headers = response.headers

                # 显式检查：即便 opener 被替换，也不接受跳转到其他站点
                self._check_url(source.allowed_host, final_url)

                body = response.read(self.max_bytes + 1)

        except HTTPError as error:
            # 3xx 也会走到这里（NoRedirect 让重定向变成 HTTPError）
            status = error.code
            error.close()

            if 300 <= status < 400:
                raise FetchError("redirect_not_allowed", status) from None

            raise FetchError(f"http_{status}", status) from None

        except URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise FetchError("timeout") from None
            raise FetchError("network_error") from None

        except TimeoutError:
            raise FetchError("timeout") from None

        except (HTTPException, OSError):
            raise FetchError("network_error") from None

        duration_ms = round((perf_counter() - started) * 1000, 3)

        if len(body) > self.max_bytes:
            raise FetchError("response_too_large", status_code)

        if not body:
            raise FetchError("empty_response", status_code)

        content_type, charset = _parse_content_type(
            headers.get("Content-Type")
        )

        return FetchResult(
            source_key=source.source_key,
            target_key=target.target_key,
            source_url=target.url,
            content_kind=target.content_kind,
            status_code=status_code,
            content_type=content_type,
            charset=charset,
            body=body,
            body_size=len(body),
            duration_ms=duration_ms,
            retrieved_at=datetime.now(timezone.utc),
        )

    # ---- 内部 ----

    def _resolve(
        self,
        source_key: str,
        target_key: str,
    ) -> tuple[object, FetchTarget]:
        """查询注册中心，并拒绝未知或已禁用的数据源。"""

        try:
            source, target = self.registry.resolve(source_key, target_key)
        except KeyError:
            raise FetchError("unknown_source_or_target") from None

        if not source.enabled:
            raise FetchError("source_disabled")

        return source, target

    @staticmethod
    def _check_url(allowed_host: str, url: str) -> None:
        """精确域名校验，拒绝任何越界地址。"""

        parsed = urlsplit(url)

        if parsed.scheme != "https":
            raise FetchError("invalid_scheme")

        if parsed.hostname != allowed_host:
            raise FetchError("host_not_allowed")
