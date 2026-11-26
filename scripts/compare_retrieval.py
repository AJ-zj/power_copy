"""05-B 对照实验：Lexical vs Dense。

用同一份已冻结评测集（retrieval-eval-v2.json）分别跑两种检索，
输岀可对比指标与逐题结果。

用法：
    python scripts/compare_retrieval.py
    python scripts/compare_retrieval.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.rag.dense import DenseSearch, DenseSearchError, build_dense_index
from powercopy.rag.lexical import LexicalSearch, build_index
from scripts.eval_retrieval import (
    classify_miss,
    complete_evidence_rate,
    evaluate,
    get_anchors,
    resolve_relevant,
)

DEFAULT_EVAL = ROOT / "evaluations" / "retrieval-eval-v2.json"
DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"
LEXICAL_DB = ROOT / "storage" / "rag" / "lexical.db"
DENSE_NPZ = ROOT / "storage" / "rag" / "dense.npz"
DENSE_CACHE = ROOT / "storage" / "rag" / "embedding-cache.json"

# 事先声明的对照目标（来自 05-A 报告）
TARGETS = {
    "H1": {
        "desc": "semantic + cross_clause 的 Recall@10 从 0 提升到 > 0.5",
        "threshold": 0.5,
    },
    "H2": {
        "desc": "exact_keyword + short_word 的 Recall@10 不低于 0.8",
        "threshold": 0.8,
    },
}


def _summarize_by_type(report: dict, types: tuple[str, ...]) -> dict:
    """合并多个类型的指标。"""

    cases = [
        c
        for c in report["cases"]
        if c["type"] in types and c.get("recall") is not None
    ]
    if not cases:
        return {"count": 0}

    return {
        "count": len(cases),
        "recall_at_k": round(
            sum(c["recall"] for c in cases) / len(cases), 4
        ),
        "hit_rate": round(
            sum(1 for c in cases if c["rr"] > 0) / len(cases), 4
        ),
        "complete_evidence_rate": round(
            sum(c["complete"] for c in cases) / len(cases), 4
        ),
    }


def _recall_at_n(
    search, cases, chunks, n: int, corpus_text: str
) -> dict:
    """按指定 N 重新计算 Recall@N。"""

    report = evaluate(search, cases, chunks, top_k=n, corpus_text=corpus_text)
    return report["summary"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Lexical vs Dense 对照实验")
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--show-cases", action="store_true")
    args = parser.parse_args()

    eval_set = json.loads(args.eval.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    chunks = manifest["chunks"]
    cases = eval_set["cases"]
    corpus_text = "\n".join(c["text"] for c in chunks)

    print("=" * 76)
    print("PowerCopy V2 · 05-B 对照实验：Lexical vs Dense")
    print("=" * 76)
    print(f"  评测集 : {args.eval.name} (v{eval_set.get('version')})")
    print(f"  语料   : {len(chunks)} 切片 / {len(manifest['documents'])} 政策")
    print(f"  用例   : {len(cases)}")
    print()

    # ---- 准备索引 ----
    if args.rebuild or not LEXICAL_DB.is_file():
        build_index(args.manifest, LEXICAL_DB)
    if args.rebuild or not DENSE_NPZ.is_file():
        build_dense_index(
            args.manifest, DENSE_NPZ, cache_path=DENSE_CACHE
        )

    lexical = LexicalSearch(LEXICAL_DB)
    dense = DenseSearch(DENSE_NPZ, manifest_path=args.manifest)

    print(f"  Lexical: {lexical.describe().get('tokenizer')} / "
          f"{lexical.chunk_count()} 切片")
    print(f"  Dense  : {dense.meta.get('model')} / "
          f"{dense.meta.get('dim')} 维 / {dense.chunk_count()} 切片")
    print()

    # ---- 三个 Top-K ----
    reports = {}
    for n in (1, 5, 10):
        lr = evaluate(lexical, cases, chunks, top_k=n, corpus_text=corpus_text)
        dr = evaluate(dense, cases, chunks, top_k=n, corpus_text=corpus_text)
        reports[n] = (lr, dr)

    lr10, dr10 = reports[10]

    # ---- 主表 ----
    print("=" * 76)
    print("一、总体指标")
    print("=" * 76)
    print(f"{'指标':26} {'Lexical':>12} {'Dense':>12} {'差值':>12}")
    print("-" * 76)

    def row(label, key, fmt="{:.4f}"):
        lv = lr10["summary"].get(key)
        dv = dr10["summary"].get(key)
        if lv is None or dv is None:
            return
        diff = dv - lv
        sign = "+" if diff >= 0 else ""
        print(
            f"{label:26} {fmt.format(lv):>12} {fmt.format(dv):>12} "
            f"{sign + fmt.format(diff):>12}"
        )

    for n in (1, 5, 10):
        lr, dr = reports[n]
        lv = lr["summary"].get("recall_at_k")
        dv = dr["summary"].get("recall_at_k")
        if lv is None:
            continue
        diff = dv - lv
        sign = "+" if diff >= 0 else ""
        print(
            f"{'Recall@' + str(n):26} {lv:>12.4f} {dv:>12.4f} "
            f"{sign + format(diff, '.4f'):>12}"
        )

    row("MRR", "mrr")
    row("nDCG@10", "ndcg_at_k")
    row("Hit Rate", "hit_rate")
    row("Complete Evidence Rate", "complete_evidence_rate")

    print()
    print(f"{'证据不足用例通过':26} "
          f"{lr10['summary'].get('empty_pass')}/{lr10['summary'].get('empty_cases'):<11} "
          f"{dr10['summary'].get('empty_pass')}/{dr10['summary'].get('empty_cases'):<11}")
    print()

    # ---- 分类 ----
    print("=" * 76)
    print("二、分类指标（Recall@10 / 完整证据率）")
    print("=" * 76)
    print(f"{'类型':22} {'n':>3} {'Lex Recall':>12} {'Dense Recall':>13} "
          f"{'Lex CER':>9} {'Dense CER':>10}")
    print("-" * 76)

    all_types = sorted(
        set(lr10["summary"].get("by_type", {}))
        | set(dr10["summary"].get("by_type", {}))
    )
    for name in all_types:
        l = lr10["summary"]["by_type"].get(name, {})
        d = dr10["summary"]["by_type"].get(name, {})
        print(
            f"{name:22} {l.get('count', 0):>3} "
            f"{l.get('recall_at_k', 0):>12.4f} "
            f"{d.get('recall_at_k', 0):>13.4f} "
            f"{l.get('complete_evidence_rate', 0):>9.4f} "
            f"{d.get('complete_evidence_rate', 0):>10.4f}"
        )
    print()

    # ---- 假设检验 ----
    print("=" * 76)
    print("三、假设检验")
    print("=" * 76)

    semantic_types = ("semantic", "cross_clause")
    l_sem = _summarize_by_type(lr10, semantic_types)
    d_sem = _summarize_by_type(dr10, semantic_types)

    lexical_types = ("exact_keyword", "short_word")
    l_lex = _summarize_by_type(lr10, lexical_types)
    d_lex = _summarize_by_type(dr10, lexical_types)

    print(f"H1: {TARGETS['H1']['desc']}")
    print(f"    Lexical: {l_sem.get('recall_at_k')}  "
          f"Dense: {d_sem.get('recall_at_k')}  "
          f"(n={l_sem.get('count')})")
    h1 = (d_sem.get("recall_at_k") or 0) > TARGETS["H1"]["threshold"]
    print(f"    判定: {'成立' if h1 else '不成立'}")
    print()

    print(f"H2: {TARGETS['H2']['desc']}")
    print(f"    Dense: {d_lex.get('recall_at_k')}  (n={l_lex.get('count')})")
    h2 = (d_lex.get("recall_at_k") or 0) >= TARGETS["H2"]["threshold"]
    print(f"    判定: {'成立' if h2 else '不成立'}")
    print()

    # ---- 未命中原因 ----
    print("=" * 76)
    print("四、未命中原因分布")
    print("=" * 76)
    print(f"{'原因':24} {'Lexical':>10} {'Dense':>10}")
    print("-" * 76)
    l_miss = lr10["summary"].get("miss_breakdown", {})
    d_miss = dr10["summary"].get("miss_breakdown", {})
    for reason in sorted(set(l_miss) | set(d_miss)):
        print(f"{reason:24} {l_miss.get(reason, 0):>10} {d_miss.get(reason, 0):>10}")
    print()

    # ---- 逐题 ----
    if args.show_cases:
        print("=" * 76)
        print("五、逐题对照")
        print("=" * 76)
        print(f"{'id':9} {'type':14} {'Lex R':>7} {'Dense R':>8} "
              f"{'Lex rank':>9} {'Dense rank':>11}  query")
        print("-" * 76)

        l_by_id = {c["id"]: c for c in lr10["cases"]}
        d_by_id = {c["id"]: c for c in dr10["cases"]}

        for case in cases:
            lc = l_by_id[case["id"]]
            dc = d_by_id[case["id"]]
            lr_v = lc.get("recall")
            dr_v = dc.get("recall")

            l_rank = min(lc.get("evidence_ranks", {}).values(), default="-")
            d_rank = min(dc.get("evidence_ranks", {}).values(), default="-")

            print(
                f"{case['id']:9} {case['type']:14} "
                f"{(lr_v if lr_v is not None else '—'):>7} "
                f"{(dr_v if dr_v is not None else '—'):>8} "
                f"{str(l_rank):>9} {str(d_rank):>11}  "
                f"{case['query'][:26]!r}"
            )
        print()

    # ---- Bad Case ----
    print("=" * 76)
    print("六、Bad Case 分析")
    print("=" * 76)
    bad = [
        c
        for c in dr10["cases"]
        if c.get("recall") == 0 and c.get("type") != "insufficient_evidence"
    ]
    if not bad:
        print("  Dense 无未命中用例")
    else:
        for c in bad:
            print(f"  {c['id']:9} {c['type']:14} {c['query'][:34]!r}")
            print(f"            相关={c.get('relevant_total')}  "
                  f"命中={c.get('relevant_found')}  "
                  f"原因={c.get('miss_reason')}")
    print()

    # ---- 写入 ----
    if args.json:
        payload = {
            "eval_set": args.eval.name,
            "eval_version": eval_set.get("version"),
            "corpus": {
                "chunks": len(chunks),
                "documents": len(manifest["documents"]),
            },
            "lexical": {
                "index": lexical.describe(),
                "at_1": reports[1][0]["summary"],
                "at_5": reports[5][0]["summary"],
                "at_10": reports[10][0]["summary"],
                "cases": reports[10][0]["cases"],
            },
            "dense": {
                "index": dense.describe(),
                "at_1": reports[1][1]["summary"],
                "at_5": reports[5][1]["summary"],
                "at_10": reports[10][1]["summary"],
                "cases": reports[10][1]["cases"],
            },
            "hypotheses": {
                "H1": {"target": TARGETS["H1"], "result": h1,
                       "lexical": l_sem, "dense": d_sem},
                "H2": {"target": TARGETS["H2"], "result": h2,
                       "dense": d_lex},
                "H3": {"status": "留到下一阶段验证（混合检索）"},
            },
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入：{args.json}")

    print("=" * 76)
    print("注：Dense 检索必然返回 Top-K，不代表证据充分。")
    print("    证据不足判断需独立机制，本阶段不设相似度阈值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
