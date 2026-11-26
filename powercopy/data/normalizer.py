"""Deterministic text and publication-date normalization."""
import re
from datetime import date
from bs4 import Comment, NavigableString, Tag


class ParseError(ValueError):
    """Invalid or ambiguous parse input; do not persist it."""


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def field_text(node: Tag) -> str:
    if node.name == "meta":
        return clean_text(str(node.get("content", "")))
    if node.name == "time" and node.get("datetime"):
        return clean_text(str(node["datetime"]))
    return clean_text(node.get_text("", strip=False))


_BLOCKS = {"p", "div", "section", "article", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "tr", "dl", "dt", "dd"}
_SKIP = {"script", "style", "noscript", "template"}


def normalize_body(node: Tag) -> str:
    parts: list[str] = []

    def walk(item):
        if isinstance(item, Comment):
            return
        if isinstance(item, NavigableString):
            parts.append(re.sub(r"\s+", " ", str(item)))
            return
        if not isinstance(item, Tag) or item.name in _SKIP:
            return
        if item.name == "br":
            parts.append("\n")
            return
        if item.name in _BLOCKS:
            parts.append("\n")
        for child in item.children:
            walk(child)
        if item.name in {"td", "th"}:
            parts.append("\t")
        if item.name in _BLOCKS:
            parts.append("\n")

    walk(node)
    lines = [clean_text(line) for line in "".join(parts).splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_published_on(value: str) -> date:
    # Only parse a deliberately selected publication-date field, never body text.
    value = clean_text(value)
    value = re.sub(r"^(?:发布日期|发布时间|公开日期|发布于)\s*[:：]?\s*", "", value)
    # Accept ISO date/time, slash/dot dates and Chinese year/month/day.
    match = re.fullmatch(
        r"(\d{4})(?:[-/.](\d{1,2})[-/.](\d{1,2})|年(\d{1,2})月(\d{1,2})日)"
        r"(?:[ T]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?", value
    )
    if not match:
        raise ParseError("INVALID_PUBLICATION_DATE")
    year, m1, d1, m2, d2 = match.groups()
    try:
        return date(int(year), int(m1 or m2), int(d1 or d2))
    except ValueError as exc:
        raise ParseError("INVALID_PUBLICATION_DATE") from exc
