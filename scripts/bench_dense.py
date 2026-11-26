"""05-B 性能测量。

测量项：
1. 模型冷启动加载时间
2. 26 个 chunk 批量向量化耗时
3. 单条 query 向量化耗时
4. 向量相似度计算耗时
5. 内存峰值
6. 索引磁盘占用

区分冷启动（首次加载）与热启动（已加载）。
"""

import json
import sys
import time
import tracemalloc

sys.path.insert(0, ".")

import numpy as np

from powercopy.rag.dense import DenseSearch, build_dense_index
from powercopy.rag.embedding import EMBEDDING_DIM, MODEL_NAME, BGEEmbedder

MANIFEST = "storage/rag/chunks.json"
INDEX = "storage/rag/dense.npz"
CACHE = "storage/rag/embedding-cache.json"

results = {}

print("=" * 70)
print("PowerCopy V2 · 05-B 性能测量")
print("=" * 70)

# ---- 1. 模型冷启动 ----
print("\n[1] 模型冷启动加载")
tracemalloc.start()
t0 = time.perf_counter()
embedder = BGEEmbedder()
embedder._ensure_loaded()
t1 = time.perf_counter()
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

cold_load_ms = (t1 - t0) * 1000
results["model_cold_load_ms"] = round(cold_load_ms, 1)
results["model_load_peak_mb"] = round(peak / 1024 / 1024, 1)
print(f"  冷启动加载: {cold_load_ms:.0f} ms")
print(f"  峰值内存  : {peak / 1024 / 1024:.1f} MB")

# ---- 2. 批量向量化（热启动，无缓存）----
manifest = json.loads(open(MANIFEST, encoding="utf-8").read())
texts = [c["text"] for c in manifest["chunks"]]
print(f"\n[2] 批量向量化 {len(texts)} 个 chunk（热启动，禁用缓存）")

# 先热一次
embedder.encode_documents(texts[:1])

runs = []
for _ in range(3):
    t0 = time.perf_counter()
    vectors = embedder.encode_documents(texts)
    t1 = time.perf_counter()
    runs.append((t1 - t0) * 1000)

batch_ms = min(runs)
results["batch_encode_26_ms"] = round(batch_ms, 1)
results["batch_encode_per_chunk_ms"] = round(batch_ms / len(texts), 2)
print(f"  最快一轮  : {batch_ms:.1f} ms")
print(f"  每条平均  : {batch_ms / len(texts):.2f} ms")
print(f"  三轮实测  : {[round(r, 1) for r in runs]}")

# ---- 3. 单条 query 向量化 ----
print("\n[3] 单条 query 向量化（热启动）")
QUERY = "电力市场注册需要什么条件"
runs = []
for _ in range(10):
    t0 = time.perf_counter()
    embedder.encode_query(QUERY)
    t1 = time.perf_counter()
    runs.append((t1 - t0) * 1000)

query_ms = sum(runs) / len(runs)
results["query_encode_ms"] = round(query_ms, 2)
results["query_encode_min_ms"] = round(min(runs), 2)
results["query_encode_max_ms"] = round(max(runs), 2)
print(f"  平均      : {query_ms:.2f} ms")
print(f"  范围      : {min(runs):.2f} - {max(runs):.2f} ms")

# ---- 4. 相似度计算 ----
print("\n[4] 向量相似度计算（纯 NumPy）")
matrix = np.asarray(vectors, dtype=np.float32)
query_vec = np.asarray(embedder.encode_query(QUERY), dtype=np.float32)

runs = []
for _ in range(100):
    t0 = time.perf_counter()
    _ = matrix @ query_vec
    t1 = time.perf_counter()
    runs.append((t1 - t0) * 1_000_000)  # 微秒

sim_us = sum(runs) / len(runs)
results["similarity_us"] = round(sim_us, 1)
print(f"  {len(texts)} x {EMBEDDING_DIM} 矩阵乘法")
print(f"  平均      : {sim_us:.1f} μs")

# ---- 5. 完整检索 ----
print("\n[5] 完整检索（含 query 编码 + 排序）")
search = DenseSearch(INDEX, manifest_path=MANIFEST, embedder=embedder)
runs = []
for _ in range(10):
    t0 = time.perf_counter()
    search.search(QUERY, top_k=10)
    t1 = time.perf_counter()
    runs.append((t1 - t0) * 1000)

full_ms = sum(runs) / len(runs)
results["full_search_ms"] = round(full_ms, 2)
print(f"  平均      : {full_ms:.2f} ms")
print(f"  范围      : {min(runs):.2f} - {max(runs):.2f} ms")

# ---- 6. 索引磁盘占用 ----
print("\n[6] 索引磁盘占用")
import os

npz_size = os.path.getsize(INDEX)
cache_size = os.path.getsize(CACHE) if os.path.exists(CACHE) else 0
results["index_bytes"] = npz_size
results["cache_bytes"] = cache_size
print(f"  dense.npz : {npz_size / 1024:.1f} KB")
print(f"  cache.json: {cache_size / 1024:.1f} KB")

# ---- 7. 冷启动检索（含索引加载）----
print("\n[7] 冷启动完整检索（含索引加载 + 模型加载）")
t0 = time.perf_counter()
cold_search = DenseSearch(INDEX, manifest_path=MANIFEST)
cold_search.search(QUERY, top_k=10)
t1 = time.perf_counter()
cold_total_ms = (t1 - t0) * 1000
results["cold_start_search_ms"] = round(cold_total_ms, 1)
print(f"  冷启动总计: {cold_total_ms:.0f} ms")

# ---- 汇总 ----
print("\n" + "=" * 70)
print("汇总（JSON）")
print("=" * 70)
results["chunks"] = len(texts)
results["dim"] = EMBEDDING_DIM
results["model"] = MODEL_NAME
print(json.dumps(results, ensure_ascii=False, indent=2))
