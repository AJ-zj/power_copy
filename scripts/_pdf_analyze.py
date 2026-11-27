"""把 OCR 接入候选准备工具。

流程
----
```text
下载 PDF
   ↓
pypdf 提取文本层 ── 成功 → pdf_text（正常路径）
   ↓ 失败（整篇无文字）
OCR（rapidocr）── 成功 → pdf_ocr（需人工核对）
   ↓ 失败
标记 unparsable
```

关键：OCR 结果**单独标记**，不伪装成文本层提取。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.data.pdf_blank_policy import describe_blank_pages  # noqa: E402
from powercopy.data.pdf_ingest import (  # noqa: E402
    MAX_BYTES,
    PdfArtifact,
    PdfIngestError,
    PdfMetadata,
    preview_pdf,
)
from powercopy.data.pdf_ocr import (  # noqa: E402
    OcrError,
    check_ocr_engine,
    describe_ocr,
    ocr_pdf_bytes,
)
from powercopy.data.versioning import _sha  # noqa: E402

STAGING = ROOT / "storage" / "pdf_staging"


def build_artifact(
    body: bytes,
    *,
    source_key: str,
    target_key: str,
    url: str,
    title: str,
) -> PdfArtifact:
    return PdfArtifact(
        fetch_id=_sha(body)[:32],
        acquisition_kind="manual_offline",
        metadata=PdfMetadata(
            source_key=source_key,
            target_key=target_key,
            source_url=url,
            title=title or "未命名政策文件",
            issuer=None,
            region=None,
            published_on=None,
            retrieved_at=datetime.now(timezone.utc),
            provenance_note=(
                f"由 prepare_pdf_candidates.py 从 {source_key}/"
                f"{target_key} 列表页发现并下载"
            ),
        ),
        raw_sha256=_sha(body),
        body_size=len(body),
        body_base64=base64.b64encode(body).decode("ascii"),
    )


def analyze(
    body: bytes,
    *,
    source_key: str,
    target_key: str,
    url: str,
    title: str,
    use_ocr: bool = True,
) -> dict:
    """分析一个 PDF：先试文本层，失败则 OCR。

    Returns:
        分析结果字典，含 `kind`（pdf_text / pdf_ocr / unparsable）
    """

    artifact = build_artifact(
        body,
        source_key=source_key,
        target_key=target_key,
        url=url,
        title=title,
    )
    artifact_path = STAGING / f"{artifact.fetch_id}.json"
    STAGING.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        artifact.model_dump_json(indent=2), encoding="utf-8"
    )

    # ---- 路径 1：文本层 ----
    try:
        preview = preview_pdf(artifact, allow_blank_pages=True)
        return {
            "kind": "pdf_text",
            "artifact": artifact_path.name,
            "fetch_id": artifact.fetch_id,
            "pages": len(preview.pages),
            "chars": len(preview.document.body_text),
            "blank_pages": list(preview.blank_pages),
            "blank_note": describe_blank_pages(list(preview.blank_pages)),
            "review_sha256": preview.review_sha256,
            "content_sha256": preview.content_sha256,
            "text_sha256": hashlib.sha256(
                preview.document.body_text.encode("utf-8")
            ).hexdigest(),
            "ocr_engine": "",
            "walkthrough": "文本层提取（确定性，无 OCR 误差）",
        }
    except PdfIngestError as extract_error:
        extract_message = str(extract_error)

    # 只有「整篇无文字」才走 OCR；其他错误（损坏、超大）OCR 也救不了
    if not extract_message.startswith("ALL_PAGES_BLANK"):
        return {
            "kind": "unparsable",
            "artifact": artifact_path.name,
            "fetch_id": artifact.fetch_id,
            "error": extract_message,
            "walkthrough": "文本层提取失败，且不属于可 OCR 的类型",
        }

    if not use_ocr:
        return {
            "kind": "unparsable",
            "artifact": artifact_path.name,
            "fetch_id": artifact.fetch_id,
            "error": extract_message + "（未启用 OCR）",
            "walkthrough": "整篇无文本层，OCR 已关闭",
        }

    available, engine_info = check_ocr_engine()
    if not available:
        return {
            "kind": "unparsable",
            "artifact": artifact_path.name,
            "fetch_id": artifact.fetch_id,
            "error": f"需要 OCR 但引擎不可用：{engine_info}",
            "walkthrough": "整篇无文本层，OCR 引擎缺失",
        }

    # ---- 路径 2：OCR ----
    try:
        ocr_result = ocr_pdf_bytes(body)
    except OcrError as ocr_error:
        return {
            "kind": "unparsable",
            "artifact": artifact_path.name,
            "fetch_id": artifact.fetch_id,
            "error": f"{extract_message}；OCR 失败：{ocr_error}",
            "walkthrough": "文本层与 OCR 均失败",
        }

    payload = ocr_result.fingerprint_payload()
    review_sha = hashlib.sha256(
        json.dumps(
            {
                "fetch_id": artifact.fetch_id,
                "raw_sha256": artifact.raw_sha256,
                **payload,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    # OCR 文本单独落盘，便于人工核对时直接看
    ocr_text_path = STAGING / f"{artifact.fetch_id}.ocr.txt"
    ocr_text_path.write_text(ocr_result.body_text, encoding="utf-8")

    return {
        "kind": "pdf_ocr",
        "artifact": artifact_path.name,
        "fetch_id": artifact.fetch_id,
        "pages": len(ocr_result.pages),
        "chars": ocr_result.total_chars,
        "blank_pages": list(ocr_result.blank_pages),
        "blank_note": describe_blank_pages(list(ocr_result.blank_pages)),
        "review_sha256": review_sha,
        "content_sha256": "",
        "text_sha256": payload["text_sha256"],
        "ocr_engine": f"{ocr_result.engine} {ocr_result.engine_version}",
        "ocr_text_file": ocr_text_path.name,
        "ocr_summary": describe_ocr(ocr_result),
        "walkthrough": (
            f"整篇无文本层（{extract_message}），已用 "
            f"{ocr_result.engine} 识别。**OCR 是概率性结果，"
            f"必须人工核对原文后才能入库**"
        ),
    }


__all__ = ["STAGING", "analyze", "build_artifact"]
