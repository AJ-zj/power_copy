"""真实原始数据的离线解析测试。

只读取 storage/raw/ 里已保存的采集文件，不重新请求官方网站。
如果本机没有这些文件，整组测试自动跳过。
"""

import json
from datetime import date
from pathlib import Path

import pytest

from powercopy.data.normalizer import ParseError
from powercopy.data.parser import (
    ParseProfile,
    load_raw,
    parse_article,
    to_policy_document,
)

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "storage" / "raw"
PROFILES_PATH = ROOT / "config" / "parser_profiles.json"

FILES = {
    "ndrc/market_rules": RAW / "722b8057489c4ac2a54461d62dad117a.json",
    "nea/registration_rules": RAW / "f5d31d6af4ab4e7fa76571bbe09bd53f.json",
    "nea_east/anhui_market_news": RAW / "9e40d80d358447a18240db6625a3c6e7.json",
    "nea_east/policy_index": RAW / "fd45e1c38564451b8e1d382360ade61c.json",
}

pytestmark = pytest.mark.skipif(
    not all(path.is_file() for path in FILES.values()) or not PROFILES_PATH.is_file(),
    reason="本机没有真实采集文件或解析配置，跳过真实数据测试",
)


@pytest.fixture(scope="module")
def profiles():
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))


def parse_key(key, profiles):
    raw = load_raw(FILES[key])
    profile = ParseProfile.model_validate(profiles[key])
    return raw, parse_article(raw, profile)


# ---------------------------------------------------------------- 配置本身
class TestProfilesConfig:
    def test_profiles_cover_all_article_targets(self, profiles):
        assert "ndrc/market_rules" in profiles
        assert "nea/registration_rules" in profiles
        assert "nea_east/anhui_market_news" in profiles

    def test_no_placeholder_selectors(self, profiles):
        for key, value in profiles.items():
            for field in ("title_selector", "body_selector"):
                assert "REPLACE_WITH" not in value[field], f"{key}.{field} 仍是占位符"

    def test_expected_title_contains_present(self, profiles):
        for key in (
            "ndrc/market_rules",
            "nea/registration_rules",
            "nea_east/anhui_market_news",
        ):
            assert profiles[key]["expected_title_contains"], key


# ---------------------------------------------------------------- NDRC
class TestNdrcPolicy:
    def test_title_and_metadata(self, profiles):
        _, article = parse_key("ndrc/market_rules", profiles)
        assert "电力市场运行基本规则" in article.title
        assert article.published_on == date(2024, 4, 25)
        assert article.issuer == "中华人民共和国国家发展和改革委员会"
        assert article.region is None
        assert article.encoding == "utf-8"

    def test_body_starts_and_ends_with_policy(self, profiles):
        _, article = parse_key("ndrc/market_rules", profiles)
        lines = article.body_text.splitlines()
        assert lines[0].startswith("《电力市场运行基本规则》")
        assert "第四十五条" in lines[-1]
        assert "2024年7月1日起施行" in lines[-1]

    def test_body_has_no_site_chrome(self, profiles):
        _, article = parse_key("ndrc/market_rules", profiles)
        for noise in (
            "下载文字版",
            "下载图片版",
            "国家发展和改革委员会发布",
            "国家发展和改革委员会规章",
        ):
            assert noise not in article.body_text, noise

    def test_clause_numbers_preserved(self, profiles):
        _, article = parse_key("ndrc/market_rules", profiles)
        for marker in ("第一条", "第二十条", "第四十五条", "第一章", "第十一章"):
            assert marker in article.body_text, marker

    def test_body_is_substantial(self, profiles):
        _, article = parse_key("ndrc/market_rules", profiles)
        assert len(article.body_text) > 3000


# ---------------------------------------------------------------- NEA
class TestNeaPolicy:
    def test_title_and_metadata(self, profiles):
        _, article = parse_key("nea/registration_rules", profiles)
        assert article.title == "国家能源局关于印发《电力市场注册基本规则》的通知"
        assert article.published_on == date(2024, 9, 13)
        assert article.issuer == "国家能源局"

    def test_hidden_seo_block_removed(self, profiles):
        """页面里隐藏的关键词行不能进入正文。"""
        _, article = parse_key("nea/registration_rules", profiles)
        assert "电力,市场,主体,经营,注册,交易,条件,材料,执行,机构" not in article.body_text

    def test_related_link_removed(self, profiles):
        """文末“答记者问”链接不属于政策正文。"""
        _, article = parse_key("nea/registration_rules", profiles)
        assert "答记者问" not in article.body_text

    def test_body_starts_with_heading(self, profiles):
        _, article = parse_key("nea/registration_rules", profiles)
        lines = article.body_text.splitlines()
        assert lines[0] == "国家能源局关于印发"
        assert "国能发监管规〔2024〕76号" in article.body_text

    def test_body_ends_with_final_clause(self, profiles):
        _, article = parse_key("nea/registration_rules", profiles)
        lines = article.body_text.splitlines()
        assert lines[-1].startswith("第五十一条")

    def test_body_has_no_table_of_metadata(self, profiles):
        _, article = parse_key("nea/registration_rules", profiles)
        for noise in ("目录项的基本信息", "公开事项名称:", "索引号:", "制发日期:"):
            assert noise not in article.body_text, noise

    def test_clause_numbers_preserved(self, profiles):
        _, article = parse_key("nea/registration_rules", profiles)
        for marker in ("第一条", "第三十条", "第五十一条", "第一章 总则", "第八章 附则"):
            assert marker in article.body_text, marker

    def test_body_is_substantial(self, profiles):
        _, article = parse_key("nea/registration_rules", profiles)
        assert len(article.body_text) > 5000


# ---------------------------------------------------------------- 市场新闻
class TestMarketNews:
    def test_parses_as_news(self, profiles):
        raw, article = parse_key("nea_east/anhui_market_news", profiles)
        assert raw.content_kind == "market_news"
        assert article.title == "安徽电力现货市场转入正式运行"
        assert article.published_on == date(2026, 9, 9)
        assert article.issuer == "国家能源局华东监管局"
        assert article.region == "安徽"

    def test_body_starts_with_news_lead(self, profiles):
        _, article = parse_key("nea_east/anhui_market_news", profiles)
        assert article.body_text.startswith("2026年9月9日")

    def test_body_has_no_nav_or_footer(self, profiles):
        _, article = parse_key("nea_east/anhui_market_news", profiles)
        for noise in ("设为首页", "网站地图", "版权所有", "扫一扫", "京ICP备"):
            assert noise not in article.body_text, noise

    def test_news_cannot_be_persisted(self, profiles):
        raw, article = parse_key("nea_east/anhui_market_news", profiles)
        with pytest.raises(ParseError, match="NOT_A_POLICY"):
            to_policy_document(raw, article)


# ---------------------------------------------------------------- 列表页
class TestPolicyIndex:
    def test_index_is_not_an_article(self, profiles):
        raw = load_raw(FILES["nea_east/policy_index"])
        assert raw.content_kind == "index"
        with pytest.raises(ParseError, match="NOT_AN_ARTICLE"):
            parse_article(raw, ParseProfile(title_selector="title", body_selector=".wrapper"))


# ---------------------------------------------------------------- 入库
class TestPersistenceIntegration:
    def test_two_policies_round_trip(self, profiles, tmp_path):
        from powercopy.data.repository import PolicyRepository

        repo = PolicyRepository(tmp_path / "policies.db")
        saved = []

        for key in ("ndrc/market_rules", "nea/registration_rules"):
            raw, article = parse_key(key, profiles)
            document = to_policy_document(raw, article)
            document_id = repo.add(document)
            saved.append(repo.get(document_id))

        assert len(saved) == 2
        assert saved[0].source_key == "ndrc"
        assert saved[1].source_key == "nea"
        assert saved[0].published_on == date(2024, 4, 25)
        assert saved[1].published_on == date(2024, 9, 13)

    def test_retrieved_at_comes_from_fetch(self, profiles, tmp_path):
        from powercopy.data.repository import PolicyRepository

        raw, article = parse_key("ndrc/market_rules", profiles)
        document = to_policy_document(raw, article)
        assert document.retrieved_at == raw.retrieved_at

        repo = PolicyRepository(tmp_path / "p.db")
        loaded = repo.get(repo.add(document))
        assert loaded.retrieved_at == raw.retrieved_at


# ---------------------------------------------------------------- 排除规则
class TestExcludeSelectors:
    def test_exclude_selectors_is_optional(self, profiles):
        profile = ParseProfile.model_validate(
            {k: v for k, v in profiles["ndrc/market_rules"].items()
             if k != "exclude_selectors"}
        )
        assert profile.exclude_selectors == ()

    def test_invalid_exclude_selector_rejected(self, profiles):
        raw = load_raw(FILES["ndrc/market_rules"])
        profile = ParseProfile.model_validate(
            {**profiles["ndrc/market_rules"], "exclude_selectors": ["["]}
        )
        with pytest.raises(ParseError, match="INVALID_SELECTOR"):
            parse_article(raw, profile)

    def test_excluding_body_selector_itself_has_no_effect(self, profiles):
        """CSS select 不包含节点自身，所以排除 body 选择器不会清空正文。

        这属于预期行为：exclude 只作用于正文容器内部。
        """
        raw = load_raw(FILES["ndrc/market_rules"])
        profile = ParseProfile.model_validate(
            {**profiles["ndrc/market_rules"], "exclude_selectors": [".zhengwen"]}
        )
        article = parse_article(raw, profile)
        assert len(article.body_text) > 3000

    def test_excluding_all_content_raises_empty_field(self, profiles):
        """把正文容器内的段落全部排除后，必须报 EMPTY_REQUIRED_FIELD。"""
        raw = load_raw(FILES["nea_east/anhui_market_news"])
        profile = ParseProfile.model_validate(
            {
                **profiles["nea_east/anhui_market_news"],
                "exclude_selectors": ["p"],
            }
        )
        # 新闻正文容器本身就是纯文本 div，排除 p 无法清空；
        # 因此用整块排除来验证空正文保护。
        profile = ParseProfile.model_validate(
            {
                **profiles["nea_east/anhui_market_news"],
                "exclude_selectors": [".view"],
            }
        )
        with pytest.raises(ParseError, match="EMPTY_REQUIRED_FIELD"):
            parse_article(raw, profile)
