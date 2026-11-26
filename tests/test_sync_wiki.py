"""docs/ → wiki/ 同步工具的测试。

重点验证链接转换：docs 用相对路径 + `.md` 后缀，
Wiki 必须用页面名（首字母大写、无后缀），直接复制会让链接全部失效。
"""

from pathlib import Path

import pytest

from scripts.sync_wiki import (
    PAGE_MAP,
    WIKI_ONLY,
    build_link_map,
    check_orphans,
    convert_links,
    sync,
    sync_images,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 链接映射
class TestLinkMap:
    def test_all_docs_pages_mapped(self):
        assert len(PAGE_MAP) == 8
        for source, target in PAGE_MAP.items():
            assert source.endswith(".md")
            assert target.endswith(".md")
            assert target[0].isupper(), f"{target} 首字母应大写"

    def test_link_map_strips_suffix(self):
        mapping = build_link_map()
        assert mapping["data-center.md"] == "Data-Center"
        assert mapping["overview.md"] == "Overview"
        assert "data-center.md" not in mapping.values()


# ---------------------------------------------------------------- 链接转换
class TestConvertLinks:
    def test_markdown_link_converted(self):
        text = "详见 [数据中心详解](data-center.md)。"
        assert convert_links(text) == "详见 [数据中心详解](Data-Center)。"

    def test_multiple_links_converted(self):
        text = "[A](overview.md) 和 [B](testing.md)"
        result = convert_links(text)
        assert "[A](Overview)" in result
        assert "[B](Testing)" in result
        assert ".md" not in result

    def test_image_path_converted(self):
        text = "![总览](screenshots/overview.png)"
        assert convert_links(text) == "![总览](images/overview.png)"

    def test_link_to_image_converted(self):
        text = "| [项目总览](screenshots/overview.png) |"
        assert convert_links(text) == "| [项目总览](images/overview.png) |"

    def test_external_link_untouched(self):
        text = "[GitHub](https://github.com/AJ-zj/power_copy)"
        assert convert_links(text) == text

    def test_anchor_link_untouched(self):
        text = "[跳转](#section)"
        assert convert_links(text) == text

    def test_code_block_content_preserved(self):
        text = "```markdown\n[示例](overview.md)\n```"
        # 代码块里的示例也转换，因为整体是文本替换；
        # 这里只确认不会破坏结构
        result = convert_links(text)
        assert "```markdown" in result
        assert result.count("```") == 2

    def test_plain_text_unchanged(self):
        text = "没有任何链接的一段文字。"
        assert convert_links(text) == text


# ---------------------------------------------------------------- 同步行为
class TestSync:
    @pytest.fixture
    def dirs(self, tmp_path):
        docs = tmp_path / "docs"
        wiki = tmp_path / "wiki"
        docs.mkdir()
        wiki.mkdir()
        (docs / "overview.md").write_text(
            "# 概览\n\n见 [测试](testing.md)。", encoding="utf-8"
        )
        (docs / "testing.md").write_text("# 测试\n", encoding="utf-8")
        return docs, wiki

    def test_creates_pages(self, dirs):
        docs, wiki = dirs
        results = sync(docs, wiki)
        assert (wiki / "Overview.md").is_file()
        assert (wiki / "Testing.md").is_file()
        statuses = {r[2] for r in results if r[2] == "CREATED"}
        assert statuses == {"CREATED"}

    def test_links_converted_on_write(self, dirs):
        docs, wiki = dirs
        sync(docs, wiki)
        content = (wiki / "Overview.md").read_text(encoding="utf-8")
        assert "[测试](Testing)" in content
        assert "testing.md" not in content

    def test_second_run_unchanged(self, dirs):
        docs, wiki = dirs
        sync(docs, wiki)
        results = sync(docs, wiki)
        for _, _, status in results:
            assert status in ("UNCHANGED", "MISSING_SOURCE")

    def test_detects_change(self, dirs):
        docs, wiki = dirs
        sync(docs, wiki)
        (docs / "overview.md").write_text("# 概览改版\n", encoding="utf-8")
        results = sync(docs, wiki)
        updated = [r for r in results if r[2] == "UPDATED"]
        assert len(updated) == 1
        assert updated[0][0] == "overview.md"

    def test_dry_run_writes_nothing(self, dirs):
        docs, wiki = dirs
        sync(docs, wiki, dry_run=True)
        assert not (wiki / "Overview.md").exists()

    def test_missing_source_reported(self, dirs):
        docs, wiki = dirs
        results = sync(docs, wiki)
        missing = [r for r in results if r[2] == "MISSING_SOURCE"]
        # 只写了 2 个文件，其余 6 个应报缺失
        assert len(missing) == 6

    def test_wiki_only_files_not_touched(self, dirs):
        docs, wiki = dirs
        home = wiki / "Home.md"
        home.write_text("# 自定义首页", encoding="utf-8")
        sync(docs, wiki)
        assert home.read_text(encoding="utf-8") == "# 自定义首页"


# ---------------------------------------------------------------- 图片同步
class TestSyncImages:
    def test_copies_png(self, tmp_path):
        docs = tmp_path / "docs"
        wiki = tmp_path / "wiki"
        (docs / "screenshots").mkdir(parents=True)
        wiki.mkdir()
        (docs / "screenshots" / "a.png").write_bytes(b"fake png data")

        results = sync_images(docs, wiki)

        assert (wiki / "images" / "a.png").is_file()
        assert results[0][2] == "CREATED"

    def test_second_run_unchanged(self, tmp_path):
        docs = tmp_path / "docs"
        wiki = tmp_path / "wiki"
        (docs / "screenshots").mkdir(parents=True)
        wiki.mkdir()
        (docs / "screenshots" / "a.png").write_bytes(b"data")

        sync_images(docs, wiki)
        results = sync_images(docs, wiki)

        assert results[0][2] == "UNCHANGED"

    def test_no_screenshots_dir(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert sync_images(docs, wiki) == []


# ---------------------------------------------------------------- 孤儿检测
class TestOrphans:
    def test_detects_unknown_file(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "Overview.md").write_text("x", encoding="utf-8")
        (wiki / "StrayPage.md").write_text("x", encoding="utf-8")

        orphans = check_orphans(wiki)

        assert "StrayPage.md" in orphans
        assert "Overview.md" not in orphans

    def test_home_and_readme_allowed(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        for name in WIKI_ONLY:
            (wiki / name).write_text("x", encoding="utf-8")
        assert check_orphans(wiki) == []


# ---------------------------------------------------------------- 真实仓库
class TestRealRepo:
    """针对真实 docs/ 与 wiki/ 的一致性检查。"""

    def test_wiki_matches_docs(self):
        docs = ROOT / "docs"
        wiki = ROOT / "wiki"
        if not docs.is_dir() or not wiki.is_dir():
            pytest.skip("缺少 docs/ 或 wiki/ 目录")

        results = sync(docs, wiki, dry_run=True)
        drifted = [r for r in results if r[2] in ("CREATED", "UPDATED")]

        assert not drifted, (
            "wiki/ 与 docs/ 不同步，请运行 python scripts/sync_wiki.py\n"
            + "\n".join(f"  {r[0]} -> {r[1]}" for r in drifted)
        )

    def test_no_orphan_pages(self):
        wiki = ROOT / "wiki"
        if not wiki.is_dir():
            pytest.skip("缺少 wiki/ 目录")
        orphans = check_orphans(wiki)
        assert not orphans, f"wiki/ 有未纳入管理的文件: {orphans}"

    def test_no_broken_relative_links(self):
        """wiki/ 里不应出现 docs 风格的相对链接。"""
        wiki = ROOT / "wiki"
        if not wiki.is_dir():
            pytest.skip("缺少 wiki/ 目录")

        offenders = []
        for path in wiki.glob("*.md"):
            if path.name in WIKI_ONLY:
                continue  # README 里有语法示例，允许
            text = path.read_text(encoding="utf-8")
            if "](screenshots/" in text:
                offenders.append(f"{path.name}: 含 screenshots/ 图片路径")
            for source in PAGE_MAP:
                if f"]({source})" in text:
                    offenders.append(f"{path.name}: 含 {source} 链接")

        assert not offenders, "wiki/ 链接未转换:\n" + "\n".join(offenders)
