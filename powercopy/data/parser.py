"""Offline parsing of the existing fetch_policy.py JSON artifact."""
import base64
import binascii
import codecs
from dataclasses import dataclass
from datetime import date
from email.message import Message
from pathlib import Path

from bs4 import BeautifulSoup, Tag
from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from soupsieve.util import SelectorSyntaxError

from powercopy.data.models import PolicyDocument
from powercopy.data.normalizer import (
    ParseError, field_text, normalize_body, normalize_published_on,
)


class RawPage(BaseModel):
    # Tolerate unrelated metadata added by the existing fetcher.
    model_config = ConfigDict(extra="ignore", frozen=True)
    fetch_id: str
    source_key: str
    target_key: str
    source_url: AnyHttpUrl
    content_kind: str
    status_code: int
    content_type: str
    charset: str | None = None
    retrieved_at: AwareDatetime
    body_size: int = Field(gt=0)
    body_base64: str

    def body_bytes(self) -> bytes:
        try:
            body = base64.b64decode(self.body_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ParseError("INVALID_BASE64") from exc
        if len(body) != self.body_size:
            raise ParseError("BODY_SIZE_MISMATCH")
        if not body.strip():
            raise ParseError("EMPTY_BODY")
        return body


def load_raw(path: str | Path) -> RawPage:
    try:
        raw = RawPage.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise ParseError("INVALID_RAW_ARTIFACT") from exc
    if raw.status_code != 200:
        raise ParseError("UNSUCCESSFUL_FETCH")
    if raw.content_type.split(";", 1)[0].strip().lower() not in {
        "text/html", "application/xhtml+xml"
    }:
        raise ParseError("UNSUPPORTED_CONTENT_TYPE")
    return raw


def _encoding(label: str) -> str:
    try:
        name = codecs.lookup(label.strip()).name
    except LookupError as exc:
        raise ParseError("UNSUPPORTED_CHARSET") from exc
    if name not in {"utf-8", "utf-8-sig", "gb2312", "gbk", "gb18030"}:
        raise ParseError("UNSUPPORTED_CHARSET")
    return "utf-8" if name == "utf-8-sig" else name


def _meta_encodings(body: bytes) -> set[str]:
    # Latin-1 is only for an ASCII-compatible metadata sniff, not the article.
    head = BeautifulSoup(body[:8192].decode("latin-1"), "html.parser")
    encodings = set()
    for meta in head.find_all("meta"):
        label = meta.get("charset")
        if not label and str(meta.get("http-equiv", "")).lower() == "content-type":
            header = Message()
            header["Content-Type"] = str(meta.get("content", ""))
            label = header.get_content_charset()
        if label:
            encodings.add(_encoding(str(label)))
    return encodings


def decode_html(body: bytes, charset: str | None) -> tuple[str, str]:
    if body.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif body.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ParseError("UNSUPPORTED_CHARSET")
    elif body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        declarations = _meta_encodings(body)
        if charset:
            declarations.add(_encoding(charset))
        if len(declarations) > 1:
            raise ParseError("CHARSET_CONFLICT")
        encoding = next(iter(declarations), "utf-8")
    try:
        return body.decode(encoding, errors="strict"), encoding
    except UnicodeError as exc:
        raise ParseError("DECODE_FAILED") from exc


class ParseProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    title_selector: str = Field(min_length=1)
    body_selector: str = Field(min_length=1)
    published_selector: str | None = None
    issuer_selector: str | None = None
    region: str | None = None
    expected_title_contains: str | None = None
    # 正文容器内需要剔除的节点（隐藏 SEO 块、文末相关链接等）。
    # 只允许显式声明的选择器，不做模糊猜测。
    exclude_selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedArticle:
    title: str
    body_text: str
    published_on: date | None
    issuer: str | None
    region: str | None
    encoding: str


def _one(soup: BeautifulSoup, selector: str) -> Tag:
    try:
        matches = soup.select(selector)
    except SelectorSyntaxError as exc:
        raise ParseError("INVALID_SELECTOR") from exc
    if len(matches) != 1:
        raise ParseError(f"SELECTOR_MATCH_COUNT:{len(matches)}:{selector}")
    return matches[0]


def parse_article(raw: RawPage, profile: ParseProfile) -> ParsedArticle:
    if raw.content_kind not in {"policy", "market_news"}:
        raise ParseError("NOT_AN_ARTICLE")
    if raw.status_code != 200:
        raise ParseError("UNSUCCESSFUL_FETCH")
    html, encoding = decode_html(raw.body_bytes(), raw.charset)
    soup = BeautifulSoup(html, "html.parser")
    title_node = _one(soup, profile.title_selector)
    body_node = _one(soup, profile.body_selector)
    if body_node is title_node or body_node.name in {"html", "body", "head", "nav", "header", "footer", "script", "style", "title"}:
        raise ParseError("INVALID_BODY_CONTAINER")
    title = field_text(title_node)
    # 在清洗前剔除配置里显式声明的干扰节点，避免混入正文。
    for selector in profile.exclude_selectors:
        try:
            noise = body_node.select(selector)
        except SelectorSyntaxError as exc:
            raise ParseError("INVALID_SELECTOR") from exc
        for node in noise:
            node.decompose()
    body = normalize_body(body_node)
    if not title or not body:
        raise ParseError("EMPTY_REQUIRED_FIELD")
    if profile.expected_title_contains and profile.expected_title_contains not in title:
        raise ParseError("UNEXPECTED_TITLE")
    published = (
        normalize_published_on(field_text(_one(soup, profile.published_selector)))
        if profile.published_selector else None
    )
    issuer = (
        field_text(_one(soup, profile.issuer_selector)) or None
        if profile.issuer_selector else None
    )
    return ParsedArticle(title, body, published, issuer, profile.region or None, encoding)


def to_policy_document(raw: RawPage, article: ParsedArticle) -> PolicyDocument:
    if raw.content_kind != "policy":
        raise ParseError("NOT_A_POLICY")
    return PolicyDocument(
        source_key=raw.source_key,
        source_url=raw.source_url,
        title=article.title,
        body_text=article.body_text,
        region=article.region,
        issuer=article.issuer,
        published_on=article.published_on,
        retrieved_at=raw.retrieved_at,
    )


def css_path(node: Tag) -> str:
    parts = []
    while isinstance(node, Tag) and node.name != "[document]":
        index = 1 + len(node.find_previous_siblings(node.name))
        parts.append(f"{node.name}:nth-of-type({index})")
        node = node.parent
    return " > ".join(reversed(parts))


def inspect_candidates(raw: RawPage) -> dict:
    html, encoding = decode_html(raw.body_bytes(), raw.charset)
    soup = BeautifulSoup(html, "html.parser")
    titles = [{"selector": css_path(n), "text": field_text(n)[:200]}
              for n in soup.select("h1,h2,h3,title,meta[name],meta[property],time")
              if field_text(n)][:40]
    bodies = []
    for n in soup.select("article,main,section,div,td"):
        text = normalize_body(n)
        if len(text) >= 80:
            bodies.append({"selector": css_path(n), "chars": len(text),
                           "head": text[:100], "tail": text[-100:]})
    bodies.sort(key=lambda item: item["chars"], reverse=True)
    # Print short metadata candidates separately; they are not automatically used.
    metadata = []
    for n in soup.select("p,span,div,td,time,meta[name]"):
        text = field_text(n)
        if 0 < len(text) <= 100 and any(k in text for k in ("发布", "公开日期", "发文机关", "发布机构", "制发机关", "来源")):
            metadata.append({"selector": css_path(n), "text": text})
    return {"source": f"{raw.source_key}/{raw.target_key}", "kind": raw.content_kind,
            "encoding": encoding, "title_candidates": titles,
            "body_candidates": bodies[:30], "metadata_candidates": metadata[:40]}
