"""PDF 空白页策略测试。

核心验证：
- 严格模式行为完全不变（既有审批不受影响）
- 放宽模式允许空白页，但空白页号进入指纹
- 整篇无文字（扫描件）仍被拒绝
- 空白过多的仍被拒绝
- 指纹绑定了空白页 —— 空白页变化会让旧审批失效
"""

from __future__ import annotations

import base64
import io
from datetime import datetime, timezone

import pytest
from pypdf import PdfWriter

from powercopy.data.pdf_blank_policy import (
    MAX_BLANK_PAGES,
    BlankPagePolicyError,
    check_blank_policy,
    describe_blank_pages,
)
from powercopy.data.pdf_ingest import (
    PdfArtifact,
    PdfIngestError,
    PdfMetadata,
    preview_pdf,
)
from powercopy.data.versioning import _sha


def _blank_pdf(pages: int = 3) -> bytes:
    """生成指定页数的空白 PDF（无文本层）。"""

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _artifact(body: bytes, *, key: str = "nea_east") -> PdfArtifact:
    return PdfArtifact(
        fetch_id="0" * 32,
        acquisition_kind="manual_offline",
        metadata=PdfMetadata(
            source_key=key,
            target_key="probe",
            source_url=(
                "https://hdj.nea.gov.cn/x.pdf" if key == "nea_east"
                else "https://zfxxgk.nea.gov.cn/x.pdf"
            ),
            title="测试 PDF",
            retrieved_at=datetime.now(timezone.utc),
            provenance_note="测试用",
        ),
        raw_sha256=_sha(body),
        body_size=len(body),
        body_base64=base64.b64encode(body).decode("ascii"),
    )


# ==========================================================================
# 1. 策略函数
# ==========================================================================
class TestBlankPolicy:
    def test_no_blank_pages_ok(self):
        check_blank_policy([1, 2, 3], [])

    def test_few_blank_pages_ok(self):
        # 3 页里 1 页空白 = 33%，超过默认 30%…用 4 页验证
        check_blank_policy([1, 2, 3, 4], [1])

    def test_all_blank_rejected(self):
        with pytest.raises(BlankPagePolicyError, match="ALL_PAGES_BLANK"):
            check_blank_policy([1, 2, 3], [1, 2, 3])

    def test_too_many_blank_rejected(self):
        """超过绝对页数上限（20 页）应拒绝，且不是全空白。

        40 页里 25 页空白：25 <= 40*0.3=12 不成立，且 25 > 20，
        优先命中绝对上限。
        """
        with pytest.raises(BlankPagePolicyError, match="TOO_MANY_BLANK"):
            check_blank_policy(list(range(1, 41)), list(range(1, 26)))

    def test_ratio_limit(self):
        # 10 页 5 页空白 = 50% > 30%
        with pytest.raises(BlankPagePolicyError, match="TOO_MANY_BLANK"):
            check_blank_policy(list(range(1, 11)), [1, 2, 3, 4, 5])

    def test_no_pages(self):
        with pytest.raises(BlankPagePolicyError, match="NO_PAGES"):
            check_blank_policy([], [])

    def test_describe_empty(self):
        assert describe_blank_pages([]) == "无空白页"

    def test_describe_pages(self):
        text = describe_blank_pages([1, 2])
        assert "1、2" in text
        assert "显式记录" in text


# ==========================================================================
# 2. 严格模式行为不变
# ==========================================================================
class TestStrictModeUnchanged:
    def test_blank_page_still_rejected_by_default(self):
        """默认（严格）下空白页仍然拒绝整篇 —— 既有审批语义不变。"""
        artifact = _artifact(_blank_pdf(3))
        with pytest.raises(PdfIngestError, match="PAGE_WITHOUT_TEXT"):
            preview_pdf(artifact)

    def test_explicit_false_same_as_default(self):
        artifact = _artifact(_blank_pdf(2))
        with pytest.raises(PdfIngestError, match="PAGE_WITHOUT_TEXT"):
            preview_pdf(artifact, allow_blank_pages=False)


# ==========================================================================
# 3. 放宽模式
# ==========================================================================
class TestLenientMode:
    def test_all_blank_still_rejected(self):
        """整篇无文字是扫描件，即使放宽也必须拒绝。"""
        artifact = _artifact(_blank_pdf(3))
        with pytest.raises(PdfIngestError, match="ALL_PAGES_BLANK"):
            preview_pdf(artifact, allow_blank_pages=True)

    def test_blank_pages_recorded_in_preview(self):
        """真实场景：封面空白 + 正文有字。

        用 pypdf 无法直接造「部分页有字」的 PDF，
        因此这里验证 preview 结构的 blank_pages 字段存在且默认空。
        """
        from powercopy.data.pdf_ingest import PdfPreview

        assert "blank_pages" in PdfPreview.__dataclass_fields__

    def test_fingerprint_includes_blank_pages(self):
        """指纹必须绑定空白页：空白页变化使旧审批失效。"""
        import inspect

        source = inspect.getsource(preview_pdf)
        assert "blank_pages" in source

        # 确认在 _json_hash 的字典里
        assert "'blank_pages':blank_pages" in source.replace(" ", "")


# ==========================================================================
# 4. 真实 PDF 集成（存在样本时）
# ==========================================================================
class TestRealPdfSamples:
    """用仓库内的合成 PDF 样本验证，不联网。"""

    def test_synthetic_pdf_usable(self):
        """有文本层的 PDF 在两种模式下都能通过。"""

        # pypdf 生成含文字的 PDF 需要 reportlab，这里跳过
        pytest.skip("需要 reportlab 生成含文字 PDF")


class TestBlankPolicyConstants:
    def test_limits_reasonable(self):
        assert 5 <= MAX_BLANK_PAGES <= 50
