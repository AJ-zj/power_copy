"""数据源注册中心（Source Registry）。

安全边界：
- 只允许访问开发者/受信任管理员预先登记的地址；
- 精确域名匹配，不做子串匹配；
- 不向 Agent 暴露动态注册能力，也不接受任意 URL。

完整的 SSRF 防御、DNS 解析检查与网络出口限制属于后续 Harness 阶段。
"""

from dataclasses import dataclass
from urllib.parse import urlsplit


class SourceConfigError(ValueError):
    """数据源配置本身有误（启动期错误，不是运行时采集错误）。"""


@dataclass(frozen=True)
class FetchTarget:
    """一个可采集的具体目标。"""

    target_key: str
    url: str
    content_kind: str


@dataclass(frozen=True)
class DataSource:
    """一个数据源，可以包含多个采集目标。"""

    source_key: str
    name: str
    allowed_host: str
    targets: tuple[FetchTarget, ...]
    enabled: bool = True


def _validate_url(url: str) -> None:
    """只接受 https，且不带账号密码、查询参数以外的可疑成分。"""

    parsed = urlsplit(url)

    if parsed.scheme != "https":
        raise SourceConfigError(
            f"只允许 https 地址：{url}"
        )

    if not parsed.hostname:
        raise SourceConfigError(f"地址缺少主机名：{url}")

    if parsed.username or parsed.password:
        raise SourceConfigError(f"地址不能包含账号密码：{url}")

    if parsed.fragment:
        raise SourceConfigError(f"地址不能包含片段标识：{url}")


class SourceRegistry:
    """统一管理数据源及其采集目标。"""

    def __init__(self):
        self._sources: dict[str, DataSource] = {}

    def register(self, source: DataSource) -> None:
        """注册数据源，检查重复 key 与地址合法性。"""

        if not source.source_key.strip():
            raise SourceConfigError("source_key 不能为空")

        if source.source_key in self._sources:
            raise SourceConfigError(
                f"duplicate source_key: {source.source_key}"
            )

        if not source.targets:
            raise SourceConfigError(
                f"数据源 {source.source_key} 至少需要一个采集目标"
            )

        target_keys: set[str] = set()

        for target in source.targets:
            if not target.target_key.strip():
                raise SourceConfigError(
                    f"{source.source_key} 存在空的 target_key"
                )

            if target.target_key in target_keys:
                raise SourceConfigError(
                    f"duplicate target_key: {target.target_key}"
                )

            target_keys.add(target.target_key)

            _validate_url(target.url)

            # 注册时就要确认 URL 属于该数据源的允许域名，
            # 避免配置错误把别的站点登记进来。
            parsed = urlsplit(target.url)
            if parsed.hostname != source.allowed_host:
                raise SourceConfigError(
                    f"{target.target_key} 的域名 {parsed.hostname} "
                    f"与 allowed_host {source.allowed_host} 不一致"
                )

        self._sources[source.source_key] = source

    def get(self, source_key: str) -> DataSource:
        """按 source_key 取数据源。"""

        if source_key not in self._sources:
            raise KeyError(f"未知数据源：{source_key}")

        return self._sources[source_key]

    def resolve(
        self,
        source_key: str,
        target_key: str,
    ) -> tuple[DataSource, FetchTarget]:
        """解析出「数据源 + 采集目标」，并校验启用状态。"""

        source = self.get(source_key)

        for target in source.targets:
            if target.target_key == target_key:
                return source, target

        raise KeyError(
            f"数据源 {source_key} 下没有采集目标：{target_key}"
        )

    def list_sources(self) -> list[str]:
        """返回已注册的数据源 key。"""

        return list(self._sources)

    def __len__(self) -> int:
        return len(self._sources)

    def __contains__(self, source_key: object) -> bool:
        return source_key in self._sources


# ---------------------------------------------------------------------------
# 首批接入的官方数据源
#
# 三个来源都在本次开发前通过网页检索人工核实过是官方站点。
# 页面仅接入「栏目首页 / 已知详情页」，不做自动发现。
# ---------------------------------------------------------------------------

NDRC = DataSource(
    source_key="ndrc",
    name="国家发展和改革委员会",
    allowed_host="zfxxgk.ndrc.gov.cn",
    targets=(
        FetchTarget(
            target_key="market_rules",
            url="https://zfxxgk.ndrc.gov.cn/web/iteminfo.jsp?id=20379",
            content_kind="policy",
        ),
        FetchTarget(
            target_key="policy_index",
            url="https://zfxxgk.ndrc.gov.cn/web/",
            content_kind="index",
        ),
    ),
)

NEA = DataSource(
    source_key="nea",
    name="国家能源局",
    allowed_host="zfxxgk.nea.gov.cn",
    targets=(
        FetchTarget(
            target_key="registration_rules",
            url="https://zfxxgk.nea.gov.cn/2024-09/13/c_1212399683.htm",
            content_kind="policy",
        ),
    ),
)

NEA_EAST = DataSource(
    source_key="nea_east",
    name="国家能源局华东监管局",
    allowed_host="hdj.nea.gov.cn",
    targets=(
        FetchTarget(
            target_key="anhui_market_news",
            url="https://hdj.nea.gov.cn/dtyw/jgdt/202609/t20260909_308441.html",
            content_kind="market_news",
        ),
        FetchTarget(
            target_key="policy_index",
            url="https://hdj.nea.gov.cn/xxgk/fdzdgknr/gfxwj/hdnyjgfxwj/",
            content_kind="index",
        ),
    ),
)


def build_source_registry() -> SourceRegistry:
    """构建默认数据源注册中心。"""

    registry = SourceRegistry()
    registry.register(NDRC)
    registry.register(NEA)
    registry.register(NEA_EAST)
    return registry
