import base64
import codecs
from datetime import datetime, timezone, date

import pytest

from powercopy.data.normalizer import ParseError, normalize_published_on
from powercopy.data.parser import (
    RawPage, ParseProfile, decode_html, load_raw, parse_article,
    to_policy_document, inspect_candidates,
)
from powercopy.data.repository import PolicyRepository

HTML = '''<html><head><meta charset="UTF-8"><title>网站标题</title></head>
<body><nav>导航干扰</nav><h1>模拟政策</h1><time>2026-11-25</time>
<div id="issuer">模拟机构</div><article><p>电力<b>市场</b>规则。</p>
<p>第一条，第二段。<br>下一行。</p><script>坏脚本</script><!--隐藏注释--></article>
<footer>网站版权</footer></body></html>'''


def raw_page(body=None, **changes):
    if body is None:
        body = HTML.encode("utf-8")
    data = dict(fetch_id="test-id", source_key="demo", target_key="policy",
                source_url="https://example.invalid/policy", content_kind="policy",
                status_code=200, content_type="text/html", charset=None,
                retrieved_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
                body_size=len(body), body_base64=base64.b64encode(body).decode())
    data.update(changes)
    return RawPage(**data)


def profile(**changes):
    data = dict(title_selector="h1", body_selector="article", published_selector="time",
                issuer_selector="#issuer", expected_title_contains="模拟政策")
    data.update(changes)
    return ParseProfile(**data)


@pytest.mark.parametrize("label", ["utf-8", "gbk", "gb18030"])
def test_declared_encodings(label):
    html = f'<meta charset="{label}">电力市场'
    assert decode_html(html.encode(label), None)[0] == html


def test_http_equiv():
    html = '<meta http-equiv="Content-Type" content="text/html; charset=gbk">电力市场'
    assert decode_html(html.encode("gbk"), None)[0] == html


def test_http_charset():
    assert decode_html("电力市场".encode("gbk"), "gbk")[0] == "电力市场"


def test_utf8_fallback():
    assert decode_html("电力市场".encode(), None)[0] == "电力市场"


def test_bom():
    assert decode_html(codecs.BOM_UTF8 + "电力".encode(), None)[0] == "电力"


@pytest.mark.parametrize("body,charset,code", [
    (b'<meta charset="gbk">', "utf-8", "CHARSET_CONFLICT"),
    (b'\xff', None, "DECODE_FAILED"),
    (b'a', "invalid-charset", "UNSUPPORTED_CHARSET"),
])
def test_decode_errors(body, charset, code):
    with pytest.raises(ParseError, match=code):
        decode_html(body, charset)


def test_raw_roundtrip(tmp_path):
    raw = raw_page()
    path = tmp_path / "raw.json"
    path.write_text(raw.model_dump_json(), encoding="utf-8")
    assert load_raw(path).body_bytes() == HTML.encode()


@pytest.mark.parametrize("changes,code", [
    ({"body_size": 1}, "BODY_SIZE_MISMATCH"),
    ({"body_base64": "!!!"}, "INVALID_BASE64"),
])
def test_raw_errors(changes, code):
    with pytest.raises(ParseError, match=code):
        raw_page(**changes).body_bytes()


def test_extract():
    article = parse_article(raw_page(), profile())
    assert article.title == "模拟政策"
    assert article.body_text == "电力市场规则。\n第一条，第二段。\n下一行。"
    assert article.issuer == "模拟机构"
    assert article.published_on == date(2026, 11, 25)


@pytest.mark.parametrize("selector,code", [("#missing", "SELECTOR_MATCH_COUNT:0"),
    ("p", "SELECTOR_MATCH_COUNT:2"), ("[", "INVALID_SELECTOR"),
    ("body", "INVALID_BODY_CONTAINER"), ("h1", "INVALID_BODY_CONTAINER")])
def test_selectors(selector, code):
    with pytest.raises(ParseError, match=code):
        parse_article(raw_page(), profile(body_selector=selector))


def test_unexpected_title():
    with pytest.raises(ParseError, match="UNEXPECTED_TITLE"):
        parse_article(raw_page(), profile(expected_title_contains="其他"))


def test_unknown_metadata():
    article = parse_article(raw_page(), profile(published_selector=None, issuer_selector=None))
    assert article.published_on is None and article.issuer is None


@pytest.mark.parametrize("text", ["2026-11-25", "发布日期：2026年11月25日", "2026/11/25 10:20:30"])
def test_dates(text):
    assert normalize_published_on(text) == date(2026, 11, 25)


@pytest.mark.parametrize("text", ["2026-02-30", "政策于2026年11月25日生效", "成文日期：2026-11-25"])
def test_invalid_dates(text):
    with pytest.raises(ParseError, match="INVALID_PUBLICATION_DATE"):
        normalize_published_on(text)


def test_index_rejected():
    with pytest.raises(ParseError, match="NOT_AN_ARTICLE"):
        parse_article(raw_page(content_kind="index"), profile())


def test_news_preview_only():
    raw = raw_page(content_kind="market_news")
    article = parse_article(raw, profile())
    with pytest.raises(ParseError, match="NOT_A_POLICY"):
        to_policy_document(raw, article)


def test_repo_integration(tmp_path):
    raw = raw_page()
    doc = to_policy_document(raw, parse_article(raw, profile()))
    assert doc.retrieved_at == raw.retrieved_at
    repo = PolicyRepository(tmp_path / "policy.db")
    assert repo.get(repo.add(doc)) == doc


def test_inspection():
    result = inspect_candidates(raw_page())
    assert result["encoding"] == "utf-8"
    assert any(item["text"] == "模拟政策" for item in result["title_candidates"])


def test_cli_preview_and_commit(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from scripts.parse_policy import main

    raw = raw_page()
    path = tmp_path / "raw.json"
    path.write_text(raw.model_dump_json(), encoding="utf-8")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"demo/policy": profile().model_dump()}), encoding="utf-8")
    db = tmp_path / "db.sqlite"
    args = ["parse_policy", "parse", str(path), "--profiles", str(profiles), "--db", str(db)]
    monkeypatch.setattr(sys, "argv", args)
    main()
    assert json.loads(capsys.readouterr().out)["title"] == "模拟政策"
    assert not db.exists()
    monkeypatch.setattr(sys, "argv", args + ["--commit"])
    main()
    result = json.loads(capsys.readouterr().out)
    assert PolicyRepository(db).get(result["document_id"]).title == "模拟政策"


def test_cli_news_commit_blocked(tmp_path, monkeypatch):
    import json
    import sys
    from scripts.parse_policy import main

    raw = raw_page(content_kind="market_news")
    path = tmp_path / "raw.json"
    path.write_text(raw.model_dump_json(), encoding="utf-8")
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"demo/policy": profile().model_dump()}), encoding="utf-8")
    db = tmp_path / "db.sqlite"
    monkeypatch.setattr(sys, "argv", ["parse_policy", "parse", str(path), "--profiles", str(profiles), "--db", str(db), "--commit"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert not db.exists()
