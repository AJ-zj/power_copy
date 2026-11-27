"""PDF OCR 兜底（扫描件处理）。

设计原则
--------
**OCR 是兜底，不是默认路径。**

1. 先用 pypdf 提取文本层（快、无误差）
2. 只有当「整篇无文本层」时才走 OCR
3. OCR 结果**必须标记来源**，因为它会出错（错字、漏字、串行）
4. OCR 文本同样进入审批指纹 —— 人工确认时确认的是 OCR 的结果

为什么必须标记
--------------
文本层提取是**确定性**的：同样的字节永远得到同样的字符。
OCR 是**概率性**的：不同引擎、不同版本、不同缩放比例都可能给出不同结果。

如果不标记，下游无法区分「这是政策原文」和「这是机器猜的」。
文案 Agent 引用 OCR 文本时，用户有权知道这一点。

OCR 错误类型（实测观察到）
--------------------------
- 全角/半角混用：`【2021]` 应为 `〔2021〕`
- 容易混淆的字：`国网` → `国冈`
- 漏字：标题中的字被漏掉

因此 OCR 文本**必须经过人工核对**才能入库，
且入库后 `source_kind` 为 `pdf_ocr` 而非 `pdf_text`。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

OCR_ENGINE_NAME = "rapidocr-onnxruntime"
OCR_DPI_SCALE = 2.0
MAX_OCR_PAGES = 100


class OcrError(ValueError):
    """OCR 处理失败。"""


@dataclass(frozen=True)
class OcrPage:
    number: int
    text: str
    line_count: int


@dataclass(frozen=True)
class OcrResult:
    pages: tuple[OcrPage, ...]
    engine: str
    engine_version: str
    dpi_scale: float
    blank_pages: tuple[int, ...]

    @property
    def body_text(self) -> str:
        return "\n\f\n".join(page.text for page in self.pages)

    @property
    def total_chars(self) -> int:
        return sum(len(page.text) for page in self.pages)

    def fingerprint_payload(self) -> dict:
        """OCR 专用指纹载荷。

        与文本层提取的指纹不同，这里额外绑定引擎与版本 ——
        换引擎或升级版本都会让旧审批失效，这是刻意的。
        """

        digest = hashlib.sha256(
            self.body_text.encode("utf-8")
        ).hexdigest()
        return {
            "kind": "pdf_ocr",
            "engine": self.engine,
            "engine_version": self.engine_version,
            "dpi_scale": self.dpi_scale,
            "text_sha256": digest,
            "blank_pages": list(self.blank_pages),
            "page_ranges": [
                (page.number, len(page.text)) for page in self.pages
            ],
        }


def check_ocr_engine() -> tuple[bool, str]:
    """检查 OCR 引擎是否可用。

    Returns:
        (可用, 版本或错误说明)
    """

    try:
        import rapidocr_onnxruntime as module

        version = getattr(module, "__version__", "unknown")
        return True, version
    except ImportError:
        return False, "未安装 rapidocr-onnxruntime"

    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def ocr_pdf_bytes(
    body: bytes,
    *,
    max_pages: int = MAX_OCR_PAGES,
    dpi_scale: float = OCR_DPI_SCALE,
) -> OcrResult:
    """对 PDF 字节做 OCR。

    只有「整篇无文本层」的 PDF 才应该调用这个函数。
    有文本层的 PDF 应优先用 `preview_pdf`。

    Raises:
        OcrError: 引擎不可用、页面过多、或全部页面识别为空
    """

    if not body.startswith(b"%PDF-"):
        raise OcrError("NOT_PDF")

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise OcrError("缺少 pypdfium2（PDF 渲染）") from exc

    available, version = check_ocr_engine()
    if not available:
        raise OcrError(version)

    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()

    try:
        document = pdfium.PdfDocument(body)
    except Exception as exc:
        raise OcrError(f"PDF 打开失败：{type(exc).__name__}") from exc

    page_count = len(document)
    if page_count == 0:
        raise OcrError("NO_PAGES")
    if page_count > max_pages:
        raise OcrError(f"PAGE_LIMIT:{page_count}>{max_pages}")

    import numpy as np

    pages: list[OcrPage] = []
    blank_pages: list[int] = []

    for index in range(page_count):
        number = index + 1
        page = document[index]
        try:
            bitmap = page.render(scale=dpi_scale)
            image = np.array(bitmap.to_pil())
        except Exception:
            blank_pages.append(number)
            continue

        try:
            result, _ = engine(image)
        except Exception:
            blank_pages.append(number)
            continue

        if not result:
            blank_pages.append(number)
            continue

        lines = [str(line[1]).strip() for line in result if line and line[1]]
        lines = [line for line in lines if line]
        if not lines:
            blank_pages.append(number)
            continue

        pages.append(
            OcrPage(
                number=number,
                text="\n".join(lines),
                line_count=len(lines),
            )
        )

    if not pages:
        raise OcrError(
            "ALL_PAGES_BLANK：OCR 未能识别出任何文字，"
            "可能是空白文档或纯图形页面"
        )

    return OcrResult(
        pages=tuple(pages),
        engine=OCR_ENGINE_NAME,
        engine_version=version,
        dpi_scale=dpi_scale,
        blank_pages=tuple(blank_pages),
    )


def describe_ocr(result: OcrResult) -> str:
    """生成人类可读的 OCR 摘要，用于审批界面。"""

    return (
        f"OCR 识别：{len(result.pages)} 页 / {result.total_chars} 字 / "
        f"引擎 {result.engine} {result.engine_version} / "
        f"缩放 {result.dpi_scale}x"
        + (
            f"；空白页 {'、'.join(str(n) for n in result.blank_pages)}"
            if result.blank_pages
            else ""
        )
    )


__all__ = [
    "MAX_OCR_PAGES",
    "OCR_DPI_SCALE",
    "OCR_ENGINE_NAME",
    "OcrError",
    "OcrPage",
    "OcrResult",
    "check_ocr_engine",
    "describe_ocr",
    "ocr_pdf_bytes",
]
