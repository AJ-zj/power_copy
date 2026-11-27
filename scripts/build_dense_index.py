"""构建向量索引（离线）。

用法：
    python scripts/build_dense_index.py
    python scripts/build_dense_index.py --rebuild
    python scripts/build_dense_index.py --manifest storage/rag/chunks.json \
        --index storage/rag/dense.npz --cache storage/rag/embedding-cache.json

说明：
- 默认完全离线（local_files_only=True），不会联网下载模型。
  首次使用前请确保模型已在 storage/models 缓存，否则会明确报错。
- 索引格式为 policy-dense-index-v2：chunk_ids 为定长 Unicode 数组，
  加载时不需要 allow_pickle，旧版对象数组索引必须重建。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.rag.dense import (  # noqa: E402
    DenseSearch,
    DenseSearchError,
    build_dense_index,
)
from powercopy.rag.embedding import BGEEmbedder, EmbeddingError  # noqa: E402

DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"
DEFAULT_INDEX = ROOT / "storage" / "rag" / "dense.npz"
DEFAULT_CACHE = ROOT / "storage" / "rag" / "embedding-cache.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="构建向量索引")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="覆盖已存在的索引（旧格式索引必须用此选项重建）",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="允许联网下载模型（默认完全离线）",
    )
    args = parser.parse_args()

    if args.index.exists() and not args.rebuild:
        print(
            f"索引已存在：{args.index}\n"
            f"如需重建请显式加 --rebuild（旧版对象数组索引无法安全加载）",
            file=sys.stderr,
        )
        return 2

    embedder = BGEEmbedder(local_files_only=not args.allow_network)

    t0 = time.perf_counter()
    try:
        stats = build_dense_index(
            args.manifest,
            args.index,
            cache_path=args.cache,
            embedder=embedder,
            overwrite=args.rebuild or not args.index.exists(),
        )
    except (DenseSearchError, EmbeddingError) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1
    elapsed = (time.perf_counter() - t0) * 1000

    print("=== 索引已构建 ===")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    print(f"  耗时: {elapsed:.0f} ms")

    # 构建后立即自检：能否用 allow_pickle=False 正常加载
    try:
        search = DenseSearch(
            args.index, manifest_path=args.manifest, embedder=embedder
        )
    except DenseSearchError as exc:
        print(f"自检失败：{exc}", file=sys.stderr)
        return 1

    print("\n=== 自检通过 ===")
    print(json.dumps(search.describe(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
