"""OCR 文本入库。

与文本层 PDF 入库的区别
------------------------
文本层 PDF 走 `ingest_pdf`，用 `preview_pdf` 得到的指纹。
OCR 文本走本模块，用 OCR 专用指纹（额外绑定引擎与版本）。

两者共用 `VersionedPolicyStore._ingest_prepared` 的版本化写入逻辑，
因此「采集 30 次、内容改 2 次」的记账方式完全一致。

审批门槛
--------
OCR 结果**必须**经人工核对后才能入库：

```python
ingest_ocr(
    store,
    artifact,
    approved_sha256="<人工从 manifest 复制的 review_sha256>",
)
```

`approved_sha256` 与 OCR 重新计算出的指纹不一致时抛
`OCR_REVIEW_REQUIRED_OR_STALE` —— 防止「审批的是旧版本」。
"""

from __future__ import annotations

import hashlib
import json

from powercopy.data.models import PolicyDocument
from powercopy.data.pdf_ingest import PdfArtifact, PdfIngestError
from powercopy.data.pdf_ocr import OcrError, ocr_pdf_bytes
from powercopy.data.versioning import (
    IngestResult,
    VersionedPolicyStore,
    _utc,
    content_hash,
)


class OcrIngestError(ValueError):
    """OCR 入库失败。"""


def compute_ocr_fingerprint(
    artifact: PdfArtifact,
    *,
    ocr_payload: dict,
) -> str:
    """计算 OCR 入库指纹。

    指纹绑定：原始字节哈希、fetch_id、OCR 引擎与版本、
    识别出的文本哈希、空白页、每页字数。
    """

    return hashlib.sha256(
        json.dumps(
            {
                "fetch_id": artifact.fetch_id,
                "raw_sha256": artifact.raw_sha256,
                **ocr_payload,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def ingest_ocr(
    store: VersionedPolicyStore,
    artifact: PdfArtifact,
    *,
    approved_sha256: str,
    body: bytes | None = None,
) -> IngestResult:
    """把 OCR 结果入库。

    Args:
        store: 版本化存储
        artifact: PDF 工件
        approved_sha256: 人工确认的指纹（来自 manifest 的 review_sha256）
        body: 可选，PDF 字节（不给时从 artifact 解码）

    Raises:
        OcrIngestError: 指纹不匹配、OCR 失败、或记录不完整
    """

    if body is None:
        body = artifact.body_bytes()

    try:
        ocr_result = ocr_pdf_bytes(body)
    except OcrError as exc:
        raise OcrIngestError(f"OCR 失败：{exc}") from exc

    payload = ocr_result.fingerprint_payload()
    fingerprint = compute_ocr_fingerprint(artifact, ocr_payload=payload)

    if approved_sha256 != fingerprint:
        raise OcrIngestError(
            "OCR_REVIEW_REQUIRED_OR_STALE：审批指纹与当前 OCR 结果不一致。"
            "可能是引擎版本变化、PDF 被替换、或审批记录过期。"
            "请重新生成清单并人工核对。"
        )

    metadata = artifact.metadata
    document = PolicyDocument(
        source_key=metadata.source_key,
        source_url=metadata.source_url,
        title=metadata.title,
        body_text=ocr_result.body_text,
        region=metadata.region,
        issuer=metadata.issuer,
        published_on=metadata.published_on,
        retrieved_at=metadata.retrieved_at,
    )

    return store._ingest_prepared(
        document,
        fetch_id=artifact.fetch_id,
        target_key=metadata.target_key,
        stamp=_utc(metadata.retrieved_at),
        raw_sha=artifact.raw_sha256,
        digest=content_hash(document),
        fingerprint=fingerprint,
    )


__all__ = [
    "OcrIngestError",
    "compute_ocr_fingerprint",
    "ingest_ocr",
]
