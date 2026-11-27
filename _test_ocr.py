"""实测 rapidocr 能否识别那 5 篇扫描件。

先小样本验证（只跑前 2 页），确认：
1. 识别出的中文是否可用
2. 单页耗时
3. 政策条款特征是否保留
"""

from __future__ import annotations

import io
import json
import ssl
import tempfile
import time
import urllib.request
from pathlib import Path

from powercopy.data.fetcher import HttpFetcher
from powercopy.data.pipeline import discover, load_raw, save_fetch
from powercopy.data.sources import build_source_registry
from rapidocr_onnxruntime import RapidOCR

ROOT = Path(__file__).resolve().parent
selector = json.loads(
    (ROOT / "config" / "index_selectors.json").read_text(encoding="utf-8")
)["nea_east/policy_index"]

registry = build_source_registry()
tmp = Path(tempfile.mkdtemp())
raw = load_raw(save_fetch(HttpFetcher(registry).fetch("nea_east", "policy_index"), tmp))
candidates = discover(raw, selector, limit=15)

targets = [
    "华东电网备用辅助服务市场运营规则",
    "绿色电力交易专章",
]

ctx = ssl.create_default_context()
headers = {
    "User-Agent": "PowerCopyAgent/0.2 (policy fetcher; contact: internal)",
    "Accept": "application/pdf,*/*",
}

print("=" * 78)
print("rapidocr 实测（每篇只跑前 2 页）")
print("=" * 78)

ocr = RapidOCR()

for item in candidates:
    if not any(t in item["title"] for t in targets):
        continue

    print(f"\n{'=' * 78}")
    print(f"{item['title'][:60]}")
    print("=" * 78)

    try:
        req = urllib.request.Request(item["url"], headers=headers)
        with urllib.request.urlopen(req, timeout=90, context=ctx) as resp:
            body = resp.read()
    except Exception as exc:
        print(f"  下载失败: {type(exc).__name__}")
        continue

    # 用 pypdfium2 渲染成图片（rapidocr 需要图像输入）
    try:
        import pypdfium2 as pdfium
    except ImportError:
        print("  缺少 pypdfium2，尝试用 pypdf 提取图片")
        continue

    pdf = pdfium.PdfDocument(body)
    print(f"  总页数: {len(pdf)}")

    for page_no in range(min(2, len(pdf))):
        page = pdf[page_no]
        bitmap = page.render(scale=2.0)
        pil_image = bitmap.to_pil()

        # 转 numpy 数组
        import numpy as np

        image_array = np.array(pil_image)

        start = time.perf_counter()
        result, elapse = ocr(image_array)
        elapsed = (time.perf_counter() - start) * 1000

        if not result:
            print(f"  第 {page_no + 1} 页: OCR 无结果 ({elapsed:.0f}ms)")
            continue

        texts = [line[1] for line in result]
        joined = "".join(texts)
        markers = sum(
            joined.count(k) for k in ("第一条", "应当", "不得", "负责", "第")
        )

        print(f"  第 {page_no + 1} 页: {len(texts)} 行 / {len(joined)} 字 / "
              f"{elapsed:.0f}ms / 特征词 {markers}")
        print(f"    识别内容: {joined[:180]}")
