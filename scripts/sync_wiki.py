"""从 docs/ 生成 wiki/ 目录。

为什么需要转换而不是直接复制
----------------------------
`docs/` 是给仓库内浏览用的，链接写 `data-center.md`（相对路径 + 小写）。
GitHub Wiki 的页面链接必须是页面名（`Data-Center`，无 `.md` 后缀、首字母大写）。

直接复制会导致 Wiki 里所有内部链接 404。所以本脚本做三件事：

1. 文件名映射：`data-center.md` → `Data-Center.md`
2. 内部链接转换：`[x](data-center.md)` → `[x](Data-Center)`
3. 图片路径转换：`screenshots/overview.png` → `images/overview.png`

`docs/` 是唯一真相源，`wiki/` 是派生产物。
`wiki/Home.md` 与 `wiki/README.md` 是 Wiki 专属文件，不由本脚本生成或覆盖。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# docs/ 文件名 → wiki/ 页面名（不含 .md）
PAGE_MAP = {
    "overview.md": "Overview.md",
    "getting-started.md": "Getting-Started.md",
    "architecture.md": "Architecture.md",
    "data-center.md": "Data-Center.md",
    "chunking.md": "Chunking.md",
    "workbench.md": "Workbench.md",
    "testing.md": "Testing.md",
    "limitations.md": "Limitations.md",
}

# 只由 Wiki 维护、不由 docs/ 生成的文件
WIKI_ONLY = {"Home.md", "README.md"}

# docs/ 里引用的图片目录 → wiki/ 里的图片目录
IMAGE_DIR_MAP = {"screenshots/": "images/"}


def build_link_map() -> dict[str, str]:
    """构造链接替换表：`data-center.md` → `Data-Center`。"""

    mapping = {}
    for source, target in PAGE_MAP.items():
        mapping[source] = target[:-3]  # 去掉 .md
    return mapping


def convert_links(text: str) -> str:
    """把 docs 风格链接转成 Wiki 页面链接。"""

    # 1. Markdown 链接 [文字](xxx.md) → [文字](PageName)
    link_map = build_link_map()

    def replace_md_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        # 只转换纯文件名链接，跳过带锚点/协议的
        if target in link_map:
            return f"[{label}]({link_map[target]})"
        return match.group(0)

    text = re.sub(r"\[([^\]]*)\]\(([^)]+)\)", replace_md_link, text)

    # 2. 图片路径 screenshots/xxx → images/xxx
    for source_dir, target_dir in IMAGE_DIR_MAP.items():
        text = text.replace(f"]({source_dir}", f"]({target_dir}")

    return text


def sync(
    docs_dir: Path,
    wiki_dir: Path,
    *,
    dry_run: bool = False,
) -> list[tuple[str, str, str]]:
    """生成 wiki 页面，返回 [(源文件, 目标文件, 状态)]。"""

    results: list[tuple[str, str, str]] = []

    for source_name, target_name in PAGE_MAP.items():
        source = docs_dir / source_name
        target = wiki_dir / target_name

        if not source.is_file():
            results.append((source_name, target_name, "MISSING_SOURCE"))
            continue

        original = source.read_text(encoding="utf-8")
        converted = convert_links(original)

        # 判断是否需要写入
        if target.is_file():
            existing = target.read_text(encoding="utf-8")
            if existing == converted:
                results.append((source_name, target_name, "UNCHANGED"))
                continue
            status = "UPDATED"
        else:
            status = "CREATED"

        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(converted, encoding="utf-8", newline="\n")

        results.append((source_name, target_name, status))

    return results


def sync_images(
    docs_dir: Path,
    wiki_dir: Path,
    *,
    dry_run: bool = False,
) -> list[tuple[str, str, str]]:
    """复制截图到 wiki/images/。"""

    source_dir = docs_dir / "screenshots"
    target_dir = wiki_dir / "images"
    results: list[tuple[str, str, str]] = []

    if not source_dir.is_dir():
        return results

    for source in sorted(source_dir.glob("*.png")):
        target = target_dir / source.name
        if target.is_file() and target.read_bytes() == source.read_bytes():
            results.append((source.name, f"images/{source.name}", "UNCHANGED"))
            continue

        status = "UPDATED" if target.is_file() else "CREATED"
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        results.append((source.name, f"images/{source.name}", status))

    return results


def check_orphans(wiki_dir: Path) -> list[str]:
    """找出 wiki/ 里不由 docs/ 生成、也不是 Wiki 专属的文件。

    这些文件不会自动更新，可能是历史遗留，需要人工确认。
    """

    allowed = set(PAGE_MAP.values()) | WIKI_ONLY
    orphans = []
    for path in wiki_dir.glob("*.md"):
        if path.name not in allowed:
            orphans.append(path.name)
    return orphans


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 docs/ 生成 wiki/（链接与文件名自动转换）"
    )
    parser.add_argument(
        "--docs", type=Path, default=ROOT / "docs", help="源目录"
    )
    parser.add_argument(
        "--wiki", type=Path, default=ROOT / "wiki", help="目标目录"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查是否同步，有差异返回非零（用于 CI）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="不写入，只报告"
    )
    args = parser.parse_args()

    if not args.docs.is_dir():
        print(f"源目录不存在: {args.docs}", file=sys.stderr)
        return 1

    dry = args.dry_run or args.check
    page_results = sync(args.docs, args.wiki, dry_run=dry)
    image_results = sync_images(args.docs, args.wiki, dry_run=dry)

    print(f"源: {args.docs}")
    print(f"目标: {args.wiki}")
    print()

    print("页面:")
    for source, target, status in page_results:
        marker = {
            "CREATED": "+",
            "UPDATED": "~",
            "UNCHANGED": "=",
            "MISSING_SOURCE": "!",
        }[status]
        print(f"  [{marker}] {source:24} -> {target:24} {status}")

    print()
    print("图片:")
    for source, target, status in image_results:
        marker = {"CREATED": "+", "UPDATED": "~", "UNCHANGED": "="}[status]
        print(f"  [{marker}] {source:26} -> {target:30} {status}")

    orphans = check_orphans(args.wiki)
    if orphans:
        print()
        print("提示: 以下文件不由 docs/ 生成（Wiki 专属或历史遗留）:")
        for name in orphans:
            print(f"  - {name}")

    changed = [r for r in page_results + image_results if r[2] in ("CREATED", "UPDATED")]
    missing = [r for r in page_results if r[2] == "MISSING_SOURCE"]

    print()
    if missing:
        print(f"结果: {len(missing)} 个源文件缺失")
        return 1
    if args.check:
        if changed:
            print(f"结果: 有 {len(changed)} 个文件未同步，请运行 python scripts/sync_wiki.py")
            return 1
        print("结果: wiki/ 已与 docs/ 同步")
        return 0
    if dry:
        print(f"结果: 干跑，{len(changed)} 个文件待写入")
    else:
        print(f"结果: 写入 {len(changed)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
