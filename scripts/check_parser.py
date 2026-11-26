"""Parser & Normalizer 十一项验收脚本。

默认使用 Mock 数据，不读写真实采集文件。
加 --real 才使用 storage/raw/ 里的真实 HTML 做离线解析验证。

用法：
    python scripts/check_parser.py           # Mock 验收
    python scripts/check_parser.py --real    # 额外验证真实数据

退出码：
    0  通过
    1  有项目未通过
"""

import base64
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.normalizer import ParseError, normalize_published_on  # noqa: E402
from powercopy.data.parser import (  # noqa: E402
    ParseProfile,
    RawPage,
    decode_html,
    load_raw,
    parse_article,
    to_policy_document,
)
from powercopy.data.repository import PolicyRepository  # noqa: E402


HTML = (
    '<html><head><meta charset="UTF-8"><title>站点标题</title></head>'
    "<body><nav>导航干扰</nav><h1>模拟政策</h1>"
    '<div id="issuer">模拟机构</div>'
    "<article><p>电力<b>市场</b>规则。</p>"
    "<p>第一条，第二段。<br>下一行。</p>"
    "<script>坏脚本</script><!--隐藏注释--></article>"
    "<footer>网站版权</footer></body></html>"
)


def make_raw(body=None, **changes):
    if body is None:
        body = HTML.encode("utf-8")
    data = dict(
        fetch_id="test-id",
        source_key="demo",
        target_key="policy",
        source_url="https://example.invalid/policy",
        content_kind="policy",
        status_code=200,
        content_type="text/html",
        charset=None,
        retrieved_at=datetime(2026, 11, 26, tzinfo=timezone.utc),
        body_size=len(body),
        body_base64=base64.b64encode(body).decode(),
    )
    data.update(changes)
    return RawPage(**data)


def make_profile(**changes):
    data = dict(
        title_selector="h1",
        body_selector="article",
        published_selector=None,
        issuer_selector="#issuer",
        expected_title_contains="模拟政策",
    )
    data.update(changes)
    return ParseProfile(**data)


def check_raw_validation() -> bool:
    print("\n[1/11] 原始 JSON、Base64 与字节长度校验")

    checks = {
        "正常还原字节": make_raw().body_bytes() == HTML.encode(),
        "长度不一致被拒绝": _raises(
            lambda: make_raw(body_size=1).body_bytes(), "BODY_SIZE_MISMATCH"
        ),
        "非法 Base64 被拒绝": _raises(
            lambda: make_raw(body_base64="!!!").body_bytes(), "INVALID_BASE64"
        ),
        "空响应被拒绝": _raises(
            lambda: make_raw(body=b"   ", body_size=3).body_bytes(), "EMPTY_BODY"
        ),
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_encodings() -> bool:
    print("\n[2/11] UTF-8 / GBK / GB18030 解码与异常")

    cases = [
        ("utf-8", "utf-8"),
        ("gbk", "gbk"),
        ("gb18030", "gb18030"),
    ]
    all_ok = True
    for label, encoding in cases:
        html = f'<meta charset="{label}">电力市场'
        try:
            text, used = decode_html(html.encode(encoding), None)
            ok = text == html and used in {encoding, "gbk", "gb18030", "utf-8"}
        except ParseError:
            ok = False
        all_ok = all_ok and ok
        print(f"  [{'x' if ok else ' '}] {label} 解码")

    bom_ok = decode_html(
        __import__("codecs").BOM_UTF8 + "电力".encode(), None
    )[0] == "电力"
    all_ok = all_ok and bom_ok
    print(f"  [{'x' if bom_ok else ' '}] UTF-8 BOM")

    conflict = _raises(
        lambda: decode_html(b'<meta charset="gbk">', "utf-8"), "CHARSET_CONFLICT"
    )
    all_ok = all_ok and conflict
    print(f"  [{'x' if conflict else ' '}] 声明冲突被拒绝")

    unsupported = _raises(
        lambda: decode_html(b"abc", "not-a-charset"), "UNSUPPORTED_CHARSET"
    )
    all_ok = all_ok and unsupported
    print(f"  [{'x' if unsupported else ' '}] 未知编码被拒绝")

    decode_fail = _raises(lambda: decode_html(b"\xff\xfe\x00", None), "DECODE_FAILED")
    all_ok = all_ok and bool(decode_fail)
    print(f"  [{'x' if decode_fail else ' '}] 严格解码失败被拒绝")

    return all_ok


def check_mock_selectors() -> bool:
    print("\n[3/11] 选择器匹配（Mock HTML）")

    article = parse_article(make_raw(), make_profile())
    all_ok = True

    zero = _raises(
        lambda: parse_article(make_raw(), make_profile(body_selector="#missing")),
        "SELECTOR_MATCH_COUNT:0",
    )
    all_ok = all_ok and zero
    print(f"  [{'x' if zero else ' '}] 零匹配被拒绝")

    multi = _raises(
        lambda: parse_article(make_raw(), make_profile(body_selector="p")),
        "SELECTOR_MATCH_COUNT:2",
    )
    all_ok = all_ok and multi
    print(f"  [{'x' if multi else ' '}] 多匹配被拒绝")

    invalid = _raises(
        lambda: parse_article(make_raw(), make_profile(body_selector="[")),
        "INVALID_SELECTOR",
    )
    all_ok = all_ok and invalid
    print(f"  [{'x' if invalid else ' '}] 非法选择器被拒绝")

    body_tag = _raises(
        lambda: parse_article(make_raw(), make_profile(body_selector="body")),
        "INVALID_BODY_CONTAINER",
    )
    all_ok = all_ok and body_tag
    print(f"  [{'x' if body_tag else ' '}] body 作为正文被拒绝")

    title_tag = _raises(
        lambda: parse_article(make_raw(), make_profile(body_selector="h1")),
        "INVALID_BODY_CONTAINER",
    )
    all_ok = all_ok and title_tag
    print(f"  [{'x' if title_tag else ' '}] 与标题同节点被拒绝")

    unexpected = _raises(
        lambda: parse_article(
            make_raw(), make_profile(expected_title_contains="别的标题")
        ),
        "UNEXPECTED_TITLE",
    )
    all_ok = all_ok and unexpected
    print(f"  [{'x' if unexpected else ' '}] 标题不符被拒绝")

    return all_ok


def check_body_extraction() -> bool:
    print("\n[4/11] 正文提取完整性")

    article = parse_article(make_raw(), make_profile())

    checks = {
        "标题正确": article.title == "模拟政策",
        "正文正确": article.body_text
        == "电力市场规则。\n第一条，第二段。\n下一行。",
        "内联标签已合并": "电力市场规则" in article.body_text,
        "换行保留": "\n" in article.body_text,
        "script 已排除": "坏脚本" not in article.body_text,
        "注释已排除": "隐藏注释" not in article.body_text,
        "不是整页文本": "导航干扰" not in article.body_text
        and "网站版权" not in article.body_text,
        "机构字段正确": article.issuer == "模拟机构",
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_clause_preservation() -> bool:
    print("\n[5/11] 段落、条款编号、数字、单位保留")

    html = (
        "<html><body><h1>测试政策</h1><article>"
        "<p>第一条　为规范市场，交易周期为15分钟。</p>"
        "<p>第二条　补偿标准为3元/千瓦时，下浮61.8%。</p>"
        "<p>第三条　有效期五年。</p>"
        "</article></body></html>"
    ).encode("utf-8")
    raw = make_raw(body=html)
    article = parse_article(
        raw,
        make_profile(
            expected_title_contains="测试政策",
            issuer_selector=None,
        ),
    )

    checks = {
        "条款编号保留": "第一条" in article.body_text and "第三条" in article.body_text,
        "数字保留": "15分钟" in article.body_text,
        "单位保留": "3元/千瓦时" in article.body_text,
        "百分比保留": "61.8%" in article.body_text,
        "三段保留": len(article.body_text.splitlines()) == 3,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_dates() -> bool:
    print("\n[6/11] 发布日期与未知元数据")

    ok_dates = True
    for text in ("2026-11-25", "发布日期：2026年11月25日", "2026/11/25 10:20:30"):
        try:
            good = normalize_published_on(text) == date(2026, 11, 25)
        except ParseError:
            good = False
        ok_dates = ok_dates and good
        print(f"  [{'x' if good else ' '}] 解析日期 {text!r}")

    ok_bad = True
    for text in ("2026-02-30", "政策于2026年11月25日生效", "成文日期：2026-11-25"):
        bad = _raises(
            lambda t=text: normalize_published_on(t), "INVALID_PUBLICATION_DATE"
        )
        ok_bad = ok_bad and bad
        print(f"  [{'x' if bad else ' '}] 拒绝非发布日期 {text!r}")

    unknown = parse_article(
        make_raw(), make_profile(published_selector=None, issuer_selector=None)
    )
    unknown_ok = unknown.published_on is None and unknown.issuer is None
    print(f"  [{'x' if unknown_ok else ' '}] 未提供选择器时为 None")

    return ok_dates and ok_bad and unknown_ok


def check_safe_rejection() -> bool:
    print("\n[7/11] 零匹配、多匹配与无效正文安全拒绝")

    cases = {
        "零匹配": ("#nope", "SELECTOR_MATCH_COUNT:0"),
        "多匹配": ("p", "SELECTOR_MATCH_COUNT:2"),
        "非法选择器": ("[", "INVALID_SELECTOR"),
    }
    all_ok = True
    for label, (selector, code) in cases.items():
        rejected = _raises(
            lambda s=selector: parse_article(
                make_raw(), make_profile(body_selector=s)
            ),
            code,
        )
        all_ok = all_ok and rejected
        print(f"  [{'x' if rejected else ' '}] {label} -> {code}")

    return all_ok


def check_kind_boundaries() -> bool:
    print("\n[8/11] 新闻仅预览、列表页不入库")

    news_raw = make_raw(content_kind="market_news")
    news_article = parse_article(news_raw, make_profile())
    preview_ok = bool(news_article.body_text)
    blocked = _raises(
        lambda: to_policy_document(news_raw, news_article), "NOT_A_POLICY"
    )

    index_rejected = _raises(
        lambda: parse_article(make_raw(content_kind="index"), make_profile()),
        "NOT_AN_ARTICLE",
    )

    checks = {
        "市场新闻可解析（预览）": preview_ok,
        "市场新闻禁止入库": blocked,
        "列表页拒绝解析": index_rejected,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_persistence() -> bool:
    print("\n[9/11] 入库与完整回读")

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "policies.db"
        raw = make_raw()
        document = to_policy_document(raw, parse_article(raw, make_profile(test := None) if False else make_profile()))

        repo = PolicyRepository(db)
        document_id = repo.add(document)
        loaded = repo.get(document_id)

    checks = {
        "写入返回 document_id": document_id == document.document_id,
        "回读对象一致": loaded == document,
        "retrieved_at 来自采集": document.retrieved_at == raw.retrieved_at,
        "正文完整": loaded.body_text == document.body_text,
    }
    for name, ok in checks.items():
        print(f"  [{'x' if ok else ' '}] {name}")
    return all(checks.values())


def check_real_data() -> bool:
    print("\n[10/11] 真实原始数据离线解析（--real）")

    raw_dir = ROOT / "storage" / "raw"
    profiles_path = ROOT / "config" / "parser_profiles.json"
    if not raw_dir.is_dir() or not profiles_path.is_file():
        print("  [ ] 缺少真实采集文件或配置，跳过")
        return False

    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))

    targets = [
        ("ndrc/market_rules", "722b8057489c4ac2a54461d62dad117a.json"),
        ("nea/registration_rules", "f5d31d6af4ab4e7fa76571bbe09bd53f.json"),
        ("nea_east/anhui_market_news", "9e40d80d358447a18240db6625a3c6e7.json"),
    ]

    all_ok = True
    for key, filename in targets:
        path = raw_dir / filename
        if not path.is_file():
            print(f"  [ ] {key} 文件不存在")
            all_ok = False
            continue
        try:
            raw = load_raw(path)
            article = parse_article(raw, ParseProfile.model_validate(profiles[key]))
        except Exception as error:
            print(f"  [ ] {key} 解析失败：{error}")
            all_ok = False
            continue

        ok = (
            bool(article.title)
            and len(article.body_text) > 300
            and "REPLACE_WITH" not in profiles[key]["title_selector"]
        )
        all_ok = all_ok and ok
        print(
            f"  [{'x' if ok else ' '}] {key} · {len(article.body_text)} 字 · "
            f"{article.published_on} · {article.issuer}"
        )

    return all_ok


def _raises(action, code=None) -> bool:
    try:
        action()
    except ParseError as error:
        return code is None or code in str(error)
    except Exception:
        return False
    return False


def main() -> int:
    real = "--real" in sys.argv

    print("=" * 66)
    print("PowerCopy V2 · Parser & Normalizer 验收")
    print("默认使用 Mock" + ("；--real 将验证真实采集数据" if real else ""))
    print("=" * 66)

    checks = [
        ("原始 JSON/Base64/字节校验", check_raw_validation()),
        ("编码识别与异常", check_encodings()),
        ("选择器匹配", check_mock_selectors()),
        ("正文提取完整性", check_body_extraction()),
        ("条款数字单位保留", check_clause_preservation()),
        ("日期与未知元数据", check_dates()),
        ("无效正文安全拒绝", check_safe_rejection()),
        ("新闻与列表页边界", check_kind_boundaries()),
        ("入库与回读", check_persistence()),
    ]

    if real:
        checks.append(("真实数据离线解析", check_real_data()))

    passed = sum(1 for _, ok in checks if ok)
    print("\n" + "=" * 66)
    print(f"行为验收：{passed}/{len(checks)} 通过（全量回归由 pytest 保证）")
    for name, ok in checks:
        print(f"  [{'x' if ok else ' '}] {name}")
    print("=" * 66)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
