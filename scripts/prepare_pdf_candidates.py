"""把列表页发现的 PDF 候选转为待审批工件。

设计
----
**这是一个「准备」工具，不是「自动入库」工具。**

它做的事：
1. 抓列表页，发现候选
2. 下载 PDF（大小在限内、域名在白名单）
3. 生成 `PdfArtifact`（含原始字节与元数据）
4. 跑 `preview_pdf` 验证可解析
5. 输出「待审批清单」：每篇的 review_sha256、页数、字数、空白页

人要做的事：
6. 检查清单，确认内容确实是政策正文
7. 把 review_sha256 写进审批文件
8. 执行入库

**为什么不做全自动入库：**
PDF 摄取要求 `approved_sha256 == preview.review_sha256`。
这个设计的目的正是让人**在内容进入政策库之前**看一眼前几步提取对不对。
自动填 SHA 等于把这道闸门拆掉 —— 提取错误（比如抓到导航页、
页码混入正文、目录页当成正文）会静默污染政策库，
而下游文案会引用这些错误内容。

用法
----
    # 准备（下载 + 预览，不入库）
    python scripts/prepare_pdf_candidates.py --source nea_east --target policy_index

    # 准备并输出清单
    python scripts/prepare_pdf_candidates.py --json storage/pdf_staging/manifest.json

    # 入库（需要先审批）
    python scripts/prepare_pdf_candidates.py --ingest storage/pdf_staging/manifest.json \\
        --approvals config/pdf_approval.json
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.fetcher import HttpFetcher  # noqa: E402
from powercopy.data.pdf_analyze import analyze  # noqa: E402
from powercopy.data.pdf_ingest import (  # noqa: E402
    MAX_BYTES,
    PdfArtifact,
    PdfIngestError,
    PdfMetadata,
    ingest_pdf,
)
from powercopy.data.pdf_ocr_ingest import OcrIngestError, ingest_ocr  # noqa: E402
from powercopy.data.pipeline import discover, load_raw, save_fetch  # noqa: E402
from powercopy.data.sources import build_source_registry  # noqa: E402
from powercopy.data.versioning import VersionedPolicyStore, _sha  # noqa: E402

STAGING = ROOT / "storage" / "pdf_staging"
DB = ROOT / "storage" / "policies_versioned.db"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _download(url: str, *, timeout: float = 60.0, retries: int = 3) -> bytes:
    """直接下载 PDF 字节（不走 fetcher，因为 fetcher 有 2MiB 上限）。

    带有限重试：政府站点偶发 SSL EOF / 连接重置，
    单次失败不代表资源不可用。
    """

    ctx = ssl.create_default_context()
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "PowerCopyAgent/0.2 (policy fetcher; contact: internal)",
                "Accept": "application/pdf,*/*",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=ctx
            ) as response:
                if response.status != 200:
                    raise PdfIngestError(f"HTTP_{response.status}")
                data = response.read(MAX_BYTES + 1)
            break
        except PdfIngestError:
            raise
        except Exception as exc:  # 网络层
            last_error = exc
            if attempt < retries:
                import time

                time.sleep(2 * attempt)
            continue
    else:
        raise PdfIngestError(
            f"DOWNLOAD_FAILED:{type(last_error).__name__}"
        )

    if len(data) > MAX_BYTES:
        raise PdfIngestError("PDF_SIZE_LIMIT")
    if not data.startswith(b"%PDF-"):
        raise PdfIngestError("NOT_PDF")
    return data


def prepare(
    *,
    source_key: str,
    target_key: str,
    selector: str,
    limit: int = 15,
    output: Path | None = None,
    use_ocr: bool = True,
) -> dict:
    """抓列表页，下载并预览候选 PDF。"""

    registry = build_source_registry()
    fetcher = HttpFetcher(registry)
    STAGING.mkdir(parents=True, exist_ok=True)

    print(f"抓取列表页 {source_key}/{target_key} ...")
    index_raw = load_raw(
        save_fetch(fetcher.fetch(source_key, target_key), STAGING / "_index")
    )
    candidates = discover(index_raw, selector, limit=limit)
    print(f"发现 {len(candidates)} 个候选")
    print()

    entries = []
    for index, item in enumerate(candidates, start=1):
        url = item["url"]
        title = item["title"][:200]
        record = {
            "index": index,
            "title": title,
            "url": url,
            "status": "pending",
        }

        if not url.lower().endswith(".pdf"):
            record["status"] = "skipped_not_pdf"
            entries.append(record)
            print(f"  {index:2}. [非PDF] {title[:50]}")
            continue

        try:
            body = _download(url)
        except PdfIngestError as exc:
            record["status"] = "download_failed"
            record["error"] = str(exc)
            entries.append(record)
            print(f"  {index:2}. [下载失败 {exc}] {title[:44]}")
            continue

        # 分析：先试文本层，整篇无文字才走 OCR
        analysis = analyze(
            body,
            source_key=source_key,
            target_key=target_key,
            url=url,
            title=title,
            use_ocr=use_ocr,
        )

        if analysis["kind"] == "unparsable":
            record["status"] = "unparsable"
            record["error"] = analysis.get("error", "")
            record["artifact"] = analysis["artifact"]
            record["walkthrough"] = analysis.get("walkthrough", "")
            entries.append(record)
            print(
                f"  {index:2}. [不可解析 {str(analysis.get('error'))[:26]}] "
                f"{title[:32]}"
            )
            continue

        record.update(
            {
                "status": "ready_for_review",
                "extract_kind": analysis["kind"],
                "artifact": analysis["artifact"],
                "pages": analysis["pages"],
                "chars": analysis["chars"],
                "blank_pages": analysis.get("blank_pages", []),
                "blank_note": analysis.get("blank_note", ""),
                "review_sha256": analysis["review_sha256"],
                "content_sha256": analysis.get("content_sha256", ""),
                "text_sha256": analysis.get("text_sha256", ""),
                "ocr_engine": analysis.get("ocr_engine", ""),
                "ocr_text_file": analysis.get("ocr_text_file", ""),
                "walkthrough": analysis.get("walkthrough", ""),
            }
        )
        entries.append(record)

        tag = "OCR" if analysis["kind"] == "pdf_ocr" else "文本层"
        print(
            f"  {index:2}. [待审批/{tag}] {analysis['pages']:3} 页 "
            f"{analysis['chars']:6} 字  {title[:32]}"
        )

    manifest = {
        "source_key": source_key,
        "target_key": target_key,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": entries,
        "summary": {
            "total": len(entries),
            "ready": sum(1 for e in entries if e["status"] == "ready_for_review"),
            "unparsable": sum(1 for e in entries if e["status"] == "unparsable"),
            "download_failed": sum(
                1 for e in entries if e["status"] == "download_failed"
            ),
            "skipped": sum(
                1 for e in entries if e["status"].startswith("skipped")
            ),
        },
    }

    target = output or (STAGING / "manifest.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print("=" * 70)
    print(f"待审批 {manifest['summary']['ready']} / {len(entries)}")
    print(f"清单：{target}")
    print("=" * 70)
    print()
    print("下一步：")
    print(f"  1. 检查清单中的标题与字数是否合理")
    print(f"  2. 把 review_sha256 填进审批文件：")
    print(f"     {{")
    print(f'       "<artifact>.json": {{')
    print(f'         "approved": true,')
    print(f'         "review_sha256": "<从清单复制>",')
    print(f'         "reviewer": "你的名字",')
    print(f'         "note": "已确认正文与政策一致"')
    print(f"       }}")
    print(f"     }}")
    print(f"  3. 执行入库：")
    print(f"     python scripts/prepare_pdf_candidates.py --ingest {target}")
    print()
    print("说明：不入库是刻意的。PDF 提取可能把导航、页码混入正文，")
    print("      人工过一眼才能保证政策库不被污染。")

    return manifest


def ingest(manifest_path: Path, approvals_path: Path, db: Path) -> int:
    """按审批清单入库。"""

    manifest = _read_json(manifest_path)
    approvals = _read_json(approvals_path)

    if not approvals:
        print(f"未找到审批文件：{approvals_path}", file=sys.stderr)
        print("没有任何文件会被入库。", file=sys.stderr)
        return 2

    store = VersionedPolicyStore(db)
    staging = manifest_path.parent

    done = 0
    skipped = 0
    failed = 0

    for entry in manifest.get("candidates") or []:
        if entry.get("status") != "ready_for_review":
            continue

        name = entry.get("artifact") or ""
        approval = approvals.get(name) or {}

        if approval.get("approved") is not True:
            print(f"  [跳过 无审批] {entry['title'][:44]}")
            skipped += 1
            continue

        approved_sha = approval.get("review_sha256") or ""
        if not approved_sha:
            print(f"  [跳过 缺 SHA] {entry['title'][:44]}")
            skipped += 1
            continue

        artifact_path = staging / name
        if not artifact_path.is_file():
            print(f"  [跳过 工件缺失] {entry['title'][:44]}")
            failed += 1
            continue

        try:
            from powercopy.data.pdf_ingest import load_pdf_artifact

            artifact = load_pdf_artifact(artifact_path)

            if entry.get("extract_kind") == "pdf_ocr":
                result = ingest_ocr(
                    store, artifact, approved_sha256=approved_sha
                )
                kind_tag = "OCR"
            else:
                result = ingest_pdf(
                    store,
                    artifact,
                    approved_sha256=approved_sha,
                    allow_blank_pages=True,
                )
                kind_tag = "文本"

            print(
                f"  [入库/{kind_tag}] {result.action:9} {entry['title'][:36]}"
            )
            done += 1
        except OcrIngestError as exc:
            print(f"  [OCR失败 {str(exc)[:30]}] {entry['title'][:32]}")
            failed += 1
        except PdfIngestError as exc:
            print(f"  [失败 {str(exc)[:34]}] {entry['title'][:34]}")
            failed += 1
        except Exception as exc:
            print(f"  [异常 {type(exc).__name__}] {entry['title'][:34]}")
            failed += 1

    print()
    print(f"入库 {done} / 跳过 {skipped} / 失败 {failed}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="PDF 候选准备与入库")
    parser.add_argument("--source", default="nea_east")
    parser.add_argument("--target", default="policy_index")
    parser.add_argument("--selector", default=None)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--json", type=Path, help="清单输出路径")
    parser.add_argument("--ingest", type=Path, help="按清单入库")
    parser.add_argument("--approvals", type=Path,
                        default=ROOT / "config" / "pdf_approval.json")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="整篇无文本层时不使用 OCR（默认会用）",
    )
    args = parser.parse_args()

    if args.ingest:
        return ingest(args.ingest, args.approvals, args.db)

    selectors = _read_json(ROOT / "config" / "index_selectors.json")
    selector = args.selector or selectors.get(
        f"{args.source}/{args.target}"
    )
    if not selector:
        print(
            f"未找到选择器 {args.source}/{args.target}，"
            f"请在 config/index_selectors.json 中配置",
            file=sys.stderr,
        )
        return 2

    prepare(
        source_key=args.source,
        target_key=args.target,
        selector=selector,
        limit=args.limit,
        output=args.json,
        use_ocr=not args.no_ocr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
