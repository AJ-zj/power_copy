"""05.5-A 三路对照实验：Lexical vs Dense vs Hybrid。

固定等权 RRF 基线配置：
    rank_constant=60, lexical_candidate_k=10, dense_candidate_k=10, final_top_k=10

用同一份已冻结评测集，不修改任何标准答案。

用法：
    python scripts/compare_hybrid.py
    python scripts/compare_hybrid.py --json out.json --show-cases
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.rag.dense import DenseSearch
from powercopy.rag.hybrid import (
    DEFAULT_CANDIDATE_K,
    DEFAULT_FINAL_TOP_K,
    DEFAULT_RANK_CONSTANT,
    HybridRetriever,
)
from powercopy.rag.lexical import LexicalSearch
from scripts.eval_retrieval import evaluate, get_anchors, resolve_relevant

DEFAULT_EVAL = ROOT / "evaluations" / "retrieval-eval-v2.json"
DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"
LEXICAL_DB = ROOT / "storage" / "rag" / "lexical.db"
DENSE_NPZ = ROOT / "storage" / "rag" / "dense.npz"

# 基线配置（实验前锁定）
BASELINE = {
    "rank_constant": DEFAULT_RANK_CONSTANT,
    "lexical_candidate_k": DEFAULT_CANDIDATE_K,
    "dense_candidate_k": DEFAULT_CANDIDATE_K,
    "final_top_k": DEFAULT_FINAL_TOP_K,
}

# 事先声明的假设
HYPOTHESES = {
    "H3": "混合检索优于单路检索（至少一个主指标高于 Lexical 和 Dense）",
}


class HybridAdapter:
    """把 HybridRetriever 适配成 evaluate() 需要的接口。

    `rank_constant` 是构造参数，不能传给 search()，因此单独处理。
    """

    def __init__(self, hybrid: HybridRetriever, **config):
        self.hybrid = hybrid
        self.config = {**BASELINE, **config}

    def search(self, query, *, top_k=10):
        return self.hybrid.search(
            query,
            lexical_candidate_k=self.config["lexical_candidate_k"],
            dense_candidate_k=self.config["dense_candidate_k"],
            final_top_k=top_k,
        )


def _row(label, lex, dns, hyb, key, fmt="{:.4f}"):
    lv = lex.get(key)
    dv = dns.get(key)
    hv = hyb.get(key)
    if lv is None or dv is None or hv is None:
        return None

    def mark(v, best):
        return f"{fmt.format(v)}*" if abs(v - best) < 1e-9 else fmt.format(v)

    best = max(lv, dv, hv)
    return (
        f"{label:26} {mark(lv, best):>13} {mark(dv, best):>13} "
        f"{mark(hv, best):>13}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="三路对照实验")
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--show-cases", action="store_true")
    parser.add_argument("--sweep", action="store_true", help="候选深度/常数敏感性")
    args = parser.parse_args()

    eval_set = json.loads(args.eval.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    chunks = manifest["chunks"]
    cases = eval_set["cases"]
    corpus_text = "\n".join(c["text"] for c in chunks)

    print("=" * 78)
    print("PowerCopy V2 · 05.5-A 三路对照实验")
    print("=" * 78)
    print(f"  评测集 : {args.eval.name} (v{eval_set.get('version')})")
    print(f"  语料   : {len(chunks)} 切片 / {len(manifest['documents'])} 政策")
    print(f"  用例   : {len(cases)}")
    print(f"  基线配置: rank_constant={BASELINE['rank_constant']}, "
          f"lex_k={BASELINE['lexical_candidate_k']}, "
          f"dense_k={BASELINE['dense_candidate_k']}, "
          f"final_k={BASELINE['final_top_k']}")
    print()

    lexical = LexicalSearch(LEXICAL_DB)
    dense = DenseSearch(DENSE_NPZ, manifest_path=args.manifest)
    hybrid = HybridRetriever(lexical, dense)

    consistency = hybrid.check_index_consistency()
    print(f"  索引一致性: {consistency['consistent']} — {consistency['detail']}")
    print()

    adapter = HybridAdapter(hybrid)

    # 三个 Top-K
    reports = {}
    timings = {"lexical": [], "dense": [], "hybrid": []}

    for n in (1, 5, 10):
        lr = evaluate(lexical, cases, chunks, top_k=n, corpus_text=corpus_text)
        dr = evaluate(dense, cases, chunks, top_k=n, corpus_text=corpus_text)
        hr = evaluate(adapter, cases, chunks, top_k=n, corpus_text=corpus_text)
        reports[n] = (lr, dr, hr)

    # 计时
    sample_queries = [c["query"] for c in cases[:10]]
    for q in sample_queries:
        t0 = time.perf_counter(); lexical.search(q, top_k=10)
        timings["lexical"].append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter(); dense.search(q, top_k=10)
        timings["dense"].append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter(); hybrid.search(q)
        timings["hybrid"].append((time.perf_counter() - t0) * 1000)

    lr10, dr10, hr10 = reports[10]

    # ---- 主表 ----
    print("=" * 78)
    print("一、总体指标（* 表示三者中最优）")
    print("=" * 78)
    print(f"{'指标':26} {'Lexical':>13} {'Dense':>13} {'Hybrid':>13}")
    print("-" * 78)

    for n in (1, 5, 10):
        l, d, h = reports[n]
        line = _row(
            f"Recall@{n}", l["summary"], d["summary"], h["summary"],
            "recall_at_k"
        )
        if line:
            print(line)

    for key, label in (
        ("mrr", "MRR"),
        ("ndcg_at_k", "nDCG@10"),
        ("hit_rate", "Hit Rate"),
        ("complete_evidence_rate", "Complete Evidence Rate"),
    ):
        line = _row(label, lr10["summary"], dr10["summary"], hr10["summary"], key)
        if line:
            print(line)

    print()
    print(f"{'证据不足用例通过':26} "
          f"{lr10['summary'].get('empty_pass')}/{lr10['summary'].get('empty_cases'):<12} "
          f"{dr10['summary'].get('empty_pass')}/{dr10['summary'].get('empty_cases'):<12} "
          f"{hr10['summary'].get('empty_pass')}/{hr10['summary'].get('empty_cases')}")
    print()

    # ---- 分类 ----
    print("=" * 78)
    print("二、分类指标 Recall@10")
    print("=" * 78)
    print(f"{'类型':22} {'n':>3} {'Lexical':>11} {'Dense':>11} {'Hybrid':>11}")
    print("-" * 78)

    all_types = sorted(
        set(lr10["summary"].get("by_type", {}))
        | set(dr10["summary"].get("by_type", {}))
        | set(hr10["summary"].get("by_type", {}))
    )
    type_rows = {}
    for name in all_types:
        l = lr10["summary"]["by_type"].get(name, {})
        d = dr10["summary"]["by_type"].get(name, {})
        h = hr10["summary"]["by_type"].get(name, {})
        type_rows[name] = {"lexical": l, "dense": d, "hybrid": h}

        vals = [
            l.get("recall_at_k", 0),
            d.get("recall_at_k", 0),
            h.get("recall_at_k", 0),
        ]
        best = max(vals)

        def mark(v):
            return f"{v:.4f}*" if abs(v - best) < 1e-9 else f"{v:.4f}"

        print(
            f"{name:22} {l.get('count', 0):>3} "
            f"{mark(vals[0]):>11} {mark(vals[1]):>11} {mark(vals[2]):>11}"
        )
    print()

    # ---- 假设检验 ----
    print("=" * 78)
    print("三、假设检验（H3）")
    print("=" * 78)

    def key_metrics(summary):
        return {
            "recall1": summary.get("recall_at_k"),  # 占位，实际用 at_1
            "recall10": summary.get("recall_at_k"),
            "mrr": summary.get("mrr"),
            "ndcg": summary.get("ndcg_at_k"),
            "cer": summary.get("complete_evidence_rate"),
        }

    l_at1 = reports[1][0]["summary"]["recall_at_k"]
    d_at1 = reports[1][1]["summary"]["recall_at_k"]
    h_at1 = reports[1][2]["summary"]["recall_at_k"]

    metrics = {
        "Recall@1": (l_at1, d_at1, h_at1),
        "Recall@10": (
            lr10["summary"]["recall_at_k"],
            dr10["summary"]["recall_at_k"],
            hr10["summary"]["recall_at_k"],
        ),
        "MRR": (
            lr10["summary"]["mrr"],
            dr10["summary"]["mrr"],
            hr10["summary"]["mrr"],
        ),
        "nDCG@10": (
            lr10["summary"]["ndcg_at_k"],
            dr10["summary"]["ndcg_at_k"],
            hr10["summary"]["ndcg_at_k"],
        ),
        "CER": (
            lr10["summary"]["complete_evidence_rate"],
            dr10["summary"]["complete_evidence_rate"],
            hr10["summary"]["complete_evidence_rate"],
        ),
    }

    print(f"{'指标':14} {'Lexical':>10} {'Dense':>10} {'Hybrid':>10}  "
          f"{'Hybrid vs 最佳单路':>20}")
    print("-" * 78)

    wins = 0
    for label, (l, d, h) in metrics.items():
        best_single = max(l, d)
        diff = h - best_single
        sign = "+" if diff >= 0 else ""
        if diff > 0:
            wins += 1
        print(
            f"{label:14} {l:>10.4f} {d:>10.4f} {h:>10.4f}  "
            f"{sign + format(diff, '.4f'):>20}"
        )

    print()
    print(f"H3 声明: {HYPOTHESES['H3']}")
    print(f"判定: Hybrid 在 {wins}/{len(metrics)} 个主指标上优于最佳单路")

    if wins == 0:
        print("结论: H3 不成立 —— Hybrid 未优于最佳单路")
    elif wins == len(metrics):
        print("结论: H3 成立 —— Hybrid 全面优于最佳单路")
    else:
        print("结论: H3 部分成立 —— Hybrid 部分指标更优，需按类型分析")

    print()
    print("注意：本结论基于 30 条开发集用例，未做统计显著性检验。")
    print("      不构成「显著提升」的论断，仅作为开发集观测。")
    print()

    # ---- 分类型 H3 ----
    print("=" * 78)
    print("四、分类型 H3 分析")
    print("=" * 78)
    print(f"{'类型':22} {'Lex':>8} {'Dense':>8} {'Hybrid':>8}  {'判断':>12}")
    print("-" * 78)

    for name, row in type_rows.items():
        l = row["lexical"].get("recall_at_k", 0)
        d = row["dense"].get("recall_at_k", 0)
        h = row["hybrid"].get("recall_at_k", 0)
        best = max(l, d)

        if abs(h - best) < 1e-9:
            verdict = "持平"
        elif h > best:
            verdict = "更优"
        else:
            verdict = "下降"

        print(f"{name:22} {l:>8.4f} {d:>8.4f} {h:>8.4f}  {verdict:>12}")
    print()

    # ---- 耗时 ----
    print("=" * 78)
    print("五、耗时（10 条查询平均）")
    print("=" * 78)
    for path, values in timings.items():
        avg = sum(values) / len(values)
        print(f"  {path:8} 平均 {avg:8.2f} ms   范围 {min(values):.1f} - {max(values):.1f}")
    print()

    # ---- Bad Case ----
    print("=" * 78)
    print("六、Bad Case：Hybrid 相对单路下降")
    print("=" * 78)

    l_by_id = {c["id"]: c for c in lr10["cases"]}
    d_by_id = {c["id"]: c for c in dr10["cases"]}
    h_by_id = {c["id"]: c for c in hr10["cases"]}

    found = False
    for case in cases:
        cid = case["id"]
        lc = l_by_id[cid]
        dc = d_by_id[cid]
        hc = h_by_id[cid]

        if case.get("expect_empty"):
            continue

        lr_v = lc.get("recall") or 0
        dr_v = dc.get("recall") or 0
        hr_v = hc.get("recall") or 0
        best = max(lr_v, dr_v)

        if hr_v < best - 1e-9:
            found = True
            print(f"  {cid:9} {case['type']:14} {case['query'][:30]!r}")
            print(f"             Lexical={lr_v:.2f} Dense={dr_v:.2f} Hybrid={hr_v:.2f}")
            print(f"             相关={hc.get('relevant_total')} 命中={hc.get('relevant_found')}")

    if not found:
        print("  Hybrid 未在任何用例上低于最佳单路")
    print()

    # ---- 证据不足 ----
    print("=" * 78)
    print("七、证据不足用例：仅记录候选返回")
    print("=" * 78)
    print("  RRF 只融合排名，不判断证据充分性。以下仅记录候选情况。")
    print()
    for case in cases:
        if not case.get("expect_empty"):
            continue
        hc = h_by_id[case["id"]]
        print(f"  {case['id']:9} {case['query'][:34]!r}")
        print(f"             Hybrid returned={hc['returned']}  "
              f"（必然返回 Top-K，无证据判断能力）")
    print()

    # ---- 敏感性（可选）----
    if args.sweep:
        print("=" * 78)
        print("八、敏感性实验（开发集调参结果，非最终配置）")
        print("=" * 78)
        print(f"{'配置':38} {'Recall@10':>10} {'MRR':>10} {'CER':>10}")
        print("-" * 78)

        sweeps = [
            ("rank_constant=10", {"rank_constant": 10}),
            ("rank_constant=30", {"rank_constant": 30}),
            ("rank_constant=60 (基线)", {}),
            ("rank_constant=100", {"rank_constant": 100}),
            ("lex_k=5", {"lexical_candidate_k": 5}),
            ("lex_k=20", {"lexical_candidate_k": 20}),
            ("dense_k=5", {"dense_candidate_k": 5}),
            ("dense_k=20", {"dense_candidate_k": 20}),
        ]

        for label, override in sweeps:
            if "rank_constant" in override:
                # rank_constant 是构造参数，需要新建 retriever
                from powercopy.rag.hybrid import HybridRetriever as _HR

                rc_hybrid = _HR(
                    lexical,
                    dense,
                    rank_constant=override["rank_constant"],
                )
                adapter2 = HybridAdapter(rc_hybrid)
            else:
                adapter2 = HybridAdapter(hybrid, **override)

            hr = evaluate(adapter2, cases, chunks, top_k=10, corpus_text=corpus_text)
            s = hr["summary"]
            print(
                f"{label:38} {s['recall_at_k']:>10.4f} "
                f"{s['mrr']:>10.4f} {s['complete_evidence_rate']:>10.4f}"
            )
        print()
        print("  ⚠ 以上为开发集调参结果，不代表泛化性能。")
        print()

    # ---- 逐题 ----
    if args.show_cases:
        print("=" * 78)
        print("九、逐题对照（Recall@10）")
        print("=" * 78)
        print(f"{'id':9} {'type':14} {'Lex':>6} {'Dense':>7} {'Hybrid':>7}  query")
        print("-" * 78)
        for case in cases:
            cid = case["id"]
            lc = l_by_id[cid]
            dc = d_by_id[cid]
            hc = h_by_id[cid]
            print(
                f"{cid:9} {case['type']:14} "
                f"{str(lc.get('recall', '—')):>6} "
                f"{str(dc.get('recall', '—')):>7} "
                f"{str(hc.get('recall', '—')):>7}  "
                f"{case['query'][:26]!r}"
            )
        print()

    # ---- 输出 ----
    if args.json:
        payload = {
            "eval_set": args.eval.name,
            "eval_version": eval_set.get("version"),
            "baseline_config": BASELINE,
            "corpus": {
                "chunks": len(chunks),
                "documents": len(manifest["documents"]),
            },
            "hypotheses": {
                "H3": {
                    "statement": HYPOTHESES["H3"],
                    "metrics_where_hybrid_wins": wins,
                    "metrics_total": len(metrics),
                    "significant_test": "未做统计显著性检验",
                },
            },
            "lexical": {
                "at_1": reports[1][0]["summary"],
                "at_10": lr10["summary"],
                "cases": lr10["cases"],
                "timing_ms": {
                    "avg": sum(timings["lexical"]) / len(timings["lexical"]),
                    "min": min(timings["lexical"]),
                    "max": max(timings["lexical"]),
                },
            },
            "dense": {
                "at_1": reports[1][1]["summary"],
                "at_10": dr10["summary"],
                "cases": dr10["cases"],
                "timing_ms": {
                    "avg": sum(timings["dense"]) / len(timings["dense"]),
                    "min": min(timings["dense"]),
                    "max": max(timings["dense"]),
                },
            },
            "hybrid": {
                "at_1": reports[1][2]["summary"],
                "at_5": reports[5][2]["summary"],
                "at_10": hr10["summary"],
                "cases": hr10["cases"],
                "timing_ms": {
                    "avg": sum(timings["hybrid"]) / len(timings["hybrid"]),
                    "min": min(timings["hybrid"]),
                    "max": max(timings["hybrid"]),
                },
            },
            "by_type": type_rows,
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入：{args.json}")

    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
