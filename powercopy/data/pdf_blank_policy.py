"""PDF 空白页策略：允许但必须显式记录。

背景
----
真实抓取华东监管局 15 个 PDF 时发现：8 个被 `PAGE_WITHOUT_TEXT:1` 拒绝 ——
第 1 页是红头文件的**图片封面**，后续正文页都有完整文本层。

原规则要求「每一页都必须有文字」是为了**防止静默丢内容**：如果某页
提取失败，可能是解析问题而不是真的空白，悄悄跳过就等于丢掉政策条款。
这个担心是对的，所以不能简单放开。

本模块的做法
------------
允许空白页，但把它**写进审批指纹**。操作员确认 `approved_sha256` 时，
确认的不只是「内容哈希」，还包括「第 N 页是空白的」这个事实。

于是：
- 审计链完整 —— 空白页是显式的、被人工确认过的
- 不静默丢内容 —— 若有正文页意外变空，指纹会变化，审批失效
- 仍然拒绝「整篇无文字」—— 那是扫描件，需要 OCR

安全边界
--------
- 空白页数有上限（默认 ≤ 总页数 30%，且 ≤ 20 页）
- 非空白页必须有实际内容
- 所有空白页号进入指纹
"""

from __future__ import annotations

MAX_BLANK_PAGES = 20
MAX_BLANK_RATIO = 0.30


class BlankPagePolicyError(ValueError):
    """空白页策略不通过。"""


def check_blank_policy(
    page_numbers: list[int],
    blank_pages: list[int],
    *,
    max_blank: int = MAX_BLANK_PAGES,
    max_ratio: float = MAX_BLANK_RATIO,
) -> None:
    """校验空白页是否在可接受范围。

    Args:
        page_numbers: 全部页码
        blank_pages: 空白页页码

    Raises:
        BlankPagePolicyError: 空白过多或整篇无文字
    """

    total = len(page_numbers)
    blank_count = len(blank_pages)

    if total == 0:
        raise BlankPagePolicyError("NO_PAGES")

    if blank_count == total:
        raise BlankPagePolicyError(
            "ALL_PAGES_BLANK：整篇无文字层，可能是扫描件，需要 OCR"
        )

    if blank_count > max_blank:
        raise BlankPagePolicyError(
            f"TOO_MANY_BLANK_PAGES：{blank_count} 页空白，上限 {max_blank}"
        )

    ratio = blank_count / total
    if ratio > max_ratio:
        raise BlankPagePolicyError(
            f"TOO_MANY_BLANK_PAGES：{blank_count}/{total} "
            f"= {ratio:.0%} 空白，上限 {max_ratio:.0%}"
        )


def describe_blank_pages(blank_pages: list[int]) -> str:
    """生成人类可读的空白页说明，用于审批界面与日志。"""

    if not blank_pages:
        return "无空白页"
    numbers = "、".join(str(n) for n in blank_pages)
    return f"第 {numbers} 页无文字（通常为图片封面），已在审批指纹中显式记录"


__all__ = [
    "MAX_BLANK_PAGES",
    "MAX_BLANK_RATIO",
    "BlankPagePolicyError",
    "check_blank_policy",
    "describe_blank_pages",
]
