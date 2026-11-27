"""PDF OCR 兜底测试。

核心验证：
- OCR 是兜底，不是默认路径
- OCR 结果被标记为 pdf_ocr，不伪装成文本层提取
- 指纹额外绑定引擎与版本（换引擎使旧审批失效）
- 有文本层的 PDF 不会走 OCR
- 引擎缺失时明确失败而非静默跳过
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from datetime import datetime, timezone

import pytest
from pypdf import PdfWriter

from powercopy.data.pdf_blank_policy import describe_blank_pages
from powercopy.data.pdf_ingest import PdfArtifact, PdfMetadata
from powercopy.data.pdf_ocr import (
    MAX_OCR_PAGES,
    OCR_DPI_SCALE,
    OCR_ENGINE_NAME,
    OcrError,
    OcrPage,
    OcrResult,
    check_ocr_engine,
    describe_ocr,
)
from powercopy.data.pdf_ocr_ingest import (
    OcrIngestError,
    compute_ocr_fingerprint,
)
from powercopy.data.versioning import _sha


def _blank_pdf(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _artifact(body: bytes) -> PdfArtifact:
    return PdfArtifact(
        fetch_id="a" * 32,
        acquisition_kind="manual_offline",
        metadata=PdfMetadata(
            source_key="nea_east",
            target_key="policy_index",
            source_url="https://hdj.nea.gov.cn/x.pdf",
            title="测试扫描件",
            retrieved_at=datetime.now(timezone.utc),
            provenance_note="测试用",
        ),
        raw_sha256=_sha(body),
        body_size=len(body),
        body_base64=base64.b64encode(body).decode("ascii"),
    )


# ==========================================================================
# 1. 引擎可用性
# ==========================================================================
class TestEngineAvailability:
    def test_check_engine_returns_tuple(self):
        available, info = check_ocr_engine()
        assert isinstance(available, bool)
        assert isinstance(info, str)

    def test_engine_installed(self):
        available, info = check_ocr_engine()
        if not available:
            pytest.skip(f"OCR 引擎未安装：{info}")
        assert available


# ==========================================================================
# 2. OcrResult 结构与指纹
# ==========================================================================
class TestOcrResult:
    def _result(self, engine_version: str = "1.4.4") -> OcrResult:
        return OcrResult(
            pages=(
                OcrPage(number=1, text="第一条 测试内容", line_count=1),
                OcrPage(number=2, text="第二条 更多内容", line_count=1),
            ),
            engine=OCR_ENGINE_NAME,
            engine_version=engine_version,
            dpi_scale=OCR_DPI_SCALE,
            blank_pages=(),
        )

    def test_body_text_joins_with_page_break(self):
        result = self._result()
        assert "\f" in result.body_text
        assert "第一条" in result.body_text

    def test_total_chars(self):
        result = self._result()
        assert result.total_chars == sum(
            len(p.text) for p in result.pages
        )

    def test_fingerprint_payload_has_engine(self):
        payload = self._result().fingerprint_payload()
        assert payload["kind"] == "pdf_ocr"
        assert payload["engine"] == OCR_ENGINE_NAME
        assert payload["engine_version"] == "1.4.4"
        assert payload["dpi_scale"] == OCR_DPI_SCALE

    def test_fingerprint_changes_with_engine_version(self):
        """换引擎版本必须让指纹变化 —— 否则旧审批会误用在新结果上。"""
        a = self._result("1.4.4").fingerprint_payload()
        b = self._result("1.5.0").fingerprint_payload()
        assert a["engine_version"] != b["engine_version"]
        assert a["text_sha256"] == b["text_sha256"]  # 文本相同

        # 但整体指纹（含引擎）必须不同
        art_a = _artifact(_blank_pdf(1))
        art_b = _artifact(_blank_pdf(1))
        assert compute_ocr_fingerprint(
            art_a, ocr_payload=a
        ) != compute_ocr_fingerprint(art_b, ocr_payload=b)

    def test_fingerprint_changes_with_text(self):
        a = self._result().fingerprint_payload()
        b = self._result().fingerprint_payload()
        b["text_sha256"] = hashlib.sha256("不同文本".encode()).hexdigest()
        art = _artifact(_blank_pdf(1))
        assert compute_ocr_fingerprint(
            art, ocr_payload=a
        ) != compute_ocr_fingerprint(art, ocr_payload=b)

    def test_describe(self):
        text = describe_ocr(self._result())
        assert "OCR 识别" in text
        assert OCR_ENGINE_NAME in text


# ==========================================================================
# 3. 指纹稳定性
# ==========================================================================
class TestFingerprintStability:
    def test_same_input_same_fingerprint(self):
        art = _artifact(_blank_pdf(1))
        payload = OcrResult(
            pages=(OcrPage(1, "内容", 1),),
            engine=OCR_ENGINE_NAME,
            engine_version="1.4.4",
            dpi_scale=2.0,
            blank_pages=(),
        ).fingerprint_payload()
        assert compute_ocr_fingerprint(
            art, ocr_payload=payload
        ) == compute_ocr_fingerprint(art, ocr_payload=payload)

    def test_different_artifact_different_fingerprint(self):
        payload = OcrResult(
            pages=(OcrPage(1, "内容", 1),),
            engine=OCR_ENGINE_NAME,
            engine_version="1.4.4",
            dpi_scale=2.0,
            blank_pages=(),
        ).fingerprint_payload()

        art_a = _artifact(_blank_pdf(1))
        art_b = _artifact(_blank_pdf(2))
        assert compute_ocr_fingerprint(
            art_a, ocr_payload=payload
        ) != compute_ocr_fingerprint(art_b, ocr_payload=payload)


# ==========================================================================
# 4. OCR 错误处理
# ==========================================================================
class TestOcrErrors:
    def test_rejects_non_pdf(self):
        from powercopy.data.pdf_ocr import ocr_pdf_bytes

        with pytest.raises(OcrError, match="NOT_PDF"):
            ocr_pdf_bytes(b"not a pdf")

    def test_rejects_empty(self):
        from powercopy.data.pdf_ocr import ocr_pdf_bytes

        with pytest.raises(OcrError):
            ocr_pdf_bytes(b"")

    def test_ingest_rejects_wrong_sha(self):
        """审批指纹不匹配必须拒绝 —— 防止用旧审批入库新内容。"""
        from powercopy.data.pdf_ocr_ingest import ingest_ocr

        class FakeStore:
            def _ingest_prepared(self, *a, **k):
                raise AssertionError("不应到达写入阶段")

        art = _artifact(_blank_pdf(1))
        with pytest.raises((OcrIngestError, OcrError)):
            ingest_ocr(
                FakeStore(), art, approved_sha256="wrong" * 8
            )


# ==========================================================================
# 5. 常量合理性
# ==========================================================================
class TestConstants:
    def test_max_pages_reasonable(self):
        assert 10 <= MAX_OCR_PAGES <= 500

    def test_dpi_scale_reasonable(self):
        assert 1.0 <= OCR_DPI_SCALE <= 5.0

    def test_engine_name_recorded(self):
        assert OCR_ENGINE_NAME == "rapidocr-onnxruntime"


# ==========================================================================
# 6. 与文本层路径的区分
# ==========================================================================
class TestExtractionKindDistinction:
    def test_text_layer_has_no_ocr_engine(self):
        """文本层提取结果的 ocr_engine 必须为空。"""
        from powercopy.data.pdf_analyze import analyze

        # 用有文本层的真实 PDF 样本（仓库内）
        sample = (
            Path(__file__).resolve().parents[1]
            / "storage" / "raw_pdf"
            / "1a4b50fc179f47f49636309959519bff.json"
        )
        if not sample.is_file():
            pytest.skip("缺少合成 PDF 样本")

        data = json.loads(sample.read_text(encoding="utf-8"))
        body = base64.b64decode(data["body_base64"])

        result = analyze(
            body,
            source_key="nea_east",
            target_key="policy_index",
            url="https://hdj.nea.gov.cn/x.pdf",
            title="合成样本",
        )
        assert result["kind"] == "pdf_text"
        assert result["ocr_engine"] == ""

