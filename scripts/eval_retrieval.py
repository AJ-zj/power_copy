"""检索评测脚本：对给定索引计算 Recall@K / MRR / nDCG@K。

关于「相关切片」的定义
----------------------
评测集不直接写 chunk_id，而是写 `relevant_keywords`（原文关键词）。
运行时把关键词与切片正文做字面匹配，命中即为相关。

这样做的原因（项目负责人要求）：
「保留原文证据位置，不仅依赖 Chunk ID」——重新切片会改变 chunk_id，
但原文关键词不受影响。

用法：
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --top-k 10 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.rag.lexical import (  # noqa: E402
    LexicalSearch,
    LexicalSearchError,
    build_index,
    mean_reciprocal_rank,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)

DEFAULT_EVAL = ROOT / "evaluations" / "retrieval-eval-v2.json"
DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"
DEFAULT_DB = ROOT / "storage" / "rag" / "lexical.db"


def resolve_relevant(
    keywords: list[str], chunks: list[dict]
) -> set[str]:
    """按锚点字符串在原文中的字面出现，确定相关切片集合。

    评测集用 `anchors`（条款级精确锚点）而非宽泛关键词，
    避免 relevant 集合过宽导致 Recall@K 数学上限过低。

    兼容旧字段 `relevant_keywords`。
    """

    if not keywords:
        return set()

    relevant = set()
    for chunk in chunks:
        text = chunk["text"]
        if any(keyword in text for keyword in keywords):
            relevant.add(chunk["chunk_id"])
    return relevant


def get_anchors(case: dict) -> list[str]:
    """读取用例锚点，兼容 v1 的 relevant_keywords。"""

    return case.get("anchors") or case.get("relevant_keywords") or []


def complete_evidence_rate(
    relevant: set[str], retrieved: Sequence[str], k: int
) -> float:
    """跨条款完整证据召回率（Complete Evidence Rate）。

    与 Recall@K 的区别：
    - Recall@K：按证据条数算，召回 2/3 条得 0.667
    - Complete Evidence Rate：按用例算，必须全部命中才得 1，否则 0

    对跨条款查询，「能否一次召回全部所需条款」比「召回了多少条」
    更贴近「能否完整回答问题」。因此单独统计。
    """

    if not relevant:
        raise LexicalSearchError("relevant 不能为空")

    top = set(list(retrieved[:k]))
    return 1.0 if relevant <= top else 0.0


def _evidence_provenance(
    relevant: set[str], chunks: list[dict], outcome
) -> list[dict]:
    """为每条相关证据记录溯源信息：policy_id / version / 字符偏移。"""

    by_id = {c["chunk_id"]: c for c in chunks}
    hit_by_id = {h.chunk_id: h for h in outcome.hits}

    rows = []
    for chunk_id in sorted(relevant):
        chunk = by_id.get(chunk_id)
        if chunk is None:
            continue
        hit = hit_by_id.get(chunk_id)
        rows.append(
            {
                "chunk_id": chunk_id,
                "policy_id": chunk["policy_id"],
                "document_id": chunk["document_id"],
                "version": chunk["version"],
                "page": chunk.get("page"),
                "start": chunk["start"],
                "end": chunk["end"],
                "retrieved_rank": hit.rank if hit else None,
                "retrieved": hit is not None,
            }
        )
    return rows


def classify_miss(
    case: dict,
    relevant: set[str],
    retrieved: Sequence[str],
    corpus_text: str,
    top_k: int,
) -> str | None:
    """对**未命中**用例分类。命中则返回 None。

    重要：本函数只对「确实没召回」的用例给出原因。
    之前版本对所有用例无条件分类，导致 policy_relation 即使
    Recall=1.0 也被标成 relation_not_built，与分组指标自相矛盾。

    区分四种原因：
    - corpus_missing     语料里根本没有这条政策
    - relation_not_built 需要政策关系数据才能回答（图关系推理）
    - retrieval_failure  证据文本在语料里，但检索没召回
    - no_answer          真实无答案（预期为空，且确实为空）

    注意 policy_relation 分组衡量的是「能否检索到承载关系表述的**原文片段**」，
    这是文本证据检索；「政策 A 替代政策 B」这类**图关系推理**需要单独的关系数据，
    当前未建设。两者不是同一件事，不能混为一谈。
    """

    anchors = get_anchors(case)

    # 证据不足类：预期为空
    if case.get("expect_empty"):
        return "no_answer" if not retrieved else None

    # 先判断是否真的未命中 —— 命中就不需要分类
    if relevant & set(retrieved[:top_k]):
        return None

    if not anchors:
        return "annotation_missing"

    # 锚点文本在语料中是否存在
    if not any(anchor in corpus_text for anchor in anchors):
        return "corpus_missing"

    # 证据文本在语料里但没召回
    return "retrieval_failure"


def evaluate(
    search: LexicalSearch,
    cases: list[dict],
    chunks: list[dict],
    *,
    top_k: int,
    corpus_text: str = "",
) -> dict:
    """跑完整评测。"""

    rows = []
    rr_cases = []

    if not corpus_text:
        corpus_text = "\n".join(c["text"] for c in chunks)

    for case in cases:
        relevant = resolve_relevant(get_anchors(case), chunks)
        expect_empty = bool(case.get("expect_empty"))

        outcome = search.search(case["query"], top_k=top_k)
        retrieved = [h.chunk_id for h in outcome.hits]

        # 逐题记录排名位置，便于人工审计
        ranks = {}
        for position, chunk_id in enumerate(retrieved, start=1):
            if chunk_id in relevant:
                ranks[chunk_id] = position

        provenance = _evidence_provenance(relevant, chunks, outcome)

        if expect_empty:
            # 证据不足类：期望 0 条。命中任何条目都算错。
            passed = outcome.returned == 0
            rows.append(
                {
                    "id": case["id"],
                    "type": case["type"],
                    "query": case["query"],
                    "match_mode": outcome.match_mode,
                    "returned": outcome.returned,
                    "expected_empty": True,
                    "empty_ok": passed,
                    "empty_reason": case.get("evidence"),
                    "recall": None,
                    "rr": None,
                    "ndcg": None,
                    "complete": None,
                    "evidence_ranks": {},
                    "evidence": [],
                    "top_ids": retrieved,
                }
            )
            continue

        if not relevant:
            # 关键词没命中任何片段（标注与语料不一致），记为待核对
            rows.append(
                {
                    "id": case["id"],
                    "type": case["type"],
                    "query": case["query"],
                    "match_mode": outcome.match_mode,
                    "returned": outcome.returned,
                    "expected_empty": False,
                    "empty_ok": None,
                    "recall": None,
                    "rr": None,
                    "ndcg": None,
                    "warning": "标注关键词未命中任何切片",
                }
            )
            continue
        recall = recall_at_k(relevant, retrieved, top_k)
        rr = reciprocal_rank(relevant, retrieved)
        ndcg = ndcg_at_k(relevant, retrieved, top_k)
        complete = complete_evidence_rate(relevant, retrieved, top_k)
        rr_cases.append((relevant, retrieved))

        rows.append(
            {
                "id": case["id"],
                "type": case["type"],
                "query": case["query"],
                "match_mode": outcome.match_mode,
                "returned": outcome.returned,
                "expected_empty": False,
                "empty_ok": None,
                "relevant_total": len(relevant),
                "relevant_found": len(relevant & set(retrieved)),
                "recall": round(recall, 4),
                "rr": round(rr, 4),
                "ndcg": round(ndcg, 4),
                "complete": complete,
                "evidence_ranks": ranks,
                "evidence": provenance,
                "top_ids": retrieved,
                "case_evidence_note": case.get("evidence"),
                "miss_reason": classify_miss(
                    case, relevant, retrieved, corpus_text, top_k
                ),
            }
        )

    scored = [r for r in rows if r.get("recall") is not None]
    empty_cases = [r for r in rows if r.get("expected_empty")]

    def _recall(records, k):
        if not records:
            return None
        return round(sum(r["recall"] for r in records) / len(records), 4)

    def _hit(records):
        if not records:
            return None
        return round(sum(1 for r in records if r["rr"] > 0) / len(records), 4)

    summary = {
        "cases_total": len(cases),
        "cases_scored": len(scored),
        "empty_cases": len(empty_cases),
        "empty_pass": sum(1 for r in empty_cases if r["empty_ok"]),
        "top_k": top_k,
    }

    if scored:
        summary["recall_at_k"] = _recall(scored, top_k)
        summary["mrr"] = round(mean_reciprocal_rank(rr_cases), 4)
        summary["ndcg_at_k"] = round(
            sum(r["ndcg"] for r in scored) / len(scored), 4
        )
        summary["hit_rate"] = _hit(scored)
        summary["complete_evidence_rate"] = round(
            sum(r["complete"] for r in scored) / len(scored), 4
        )
        # 多证据用例单独统计，避免被单证据用例稀释
        multi = [r for r in scored if r["relevant_total"] > 1]
        if multi:
            summary["multi_evidence_cases"] = len(multi)
            summary["multi_evidence_complete_rate"] = round(
                sum(r["complete"] for r in multi) / len(multi), 4
            )

    by_type: dict[str, list[dict]] = {}
    for row in scored:
        by_type.setdefault(row["type"], []).append(row)
    summary["by_type"] = {
        name: {
            "count": len(items),
            "recall_at_k": _recall(items, top_k),
            "hit_rate": _hit(items),
            "complete_evidence_rate": round(
                sum(i["complete"] for i in items) / len(items), 4
            ),
        }
        for name, items in sorted(by_type.items())
    }

    # 未命中原因分类统计
    miss_breakdown: dict[str, int] = {}
    for row in rows:
        reason = row.get("miss_reason")
        if reason:
            miss_breakdown[reason] = miss_breakdown.get(reason, 0) + 1
    summary["miss_breakdown"] = miss_breakdown

    return {"summary": summary, "cases": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="检索评测")
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rebuild", action="store_true", help="重建索引")
    parser.add_argument("--json", type=Path, help="把结果写入 JSON 文件")
    parser.add_argument(
        "--show-cases", action="store_true", help="打印每条用例明细"
    )
    args = parser.parse_args()

    if not args.eval.is_file():
        print(f"评测集不存在：{args.eval}", file=sys.stderr)
        return 1

    if args.rebuild or not args.db.is_file():
        try:
            stats = build_index(args.manifest, args.db)
        except LexicalSearchError as exc:
            print(f"索引构建失败：{exc}", file=sys.stderr)
            return 1
        print(f"索引已构建：{stats['chunks']} 个切片 -> {args.db}")
        print()

    eval_set = json.loads(args.eval.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    chunks = manifest["chunks"]

    search = LexicalSearch(args.db)
    report = evaluate(search, eval_set["cases"], chunks, top_k=args.top_k)

    print("=" * 72)
    print("PowerCopy V2 · 检索基线评测")
    print("=" * 72)
    print(f"  评测集      : {args.eval.name} (v{eval_set.get('version')})")
    print(f"  语料        : {len(chunks)} 个切片 / {len(manifest['documents'])} 份政策")
    print(f"  检索方式    : FTS5 trigram + BM25（短查询回退扫描）")
    print(f"  Top-K       : {args.top_k}")
    print()

    s = report["summary"]
    print("--- 总体指标 ---")
    if "recall_at_k" in s:
        print(f"  Recall@{args.top_k}   : {s['recall_at_k']:.4f}")
        print(f"  MRR           : {s['mrr']:.4f}")
        print(f"  nDCG@{args.top_k}     : {s['ndcg_at_k']:.4f}")
        print(f"  命中率        : {s['hit_rate']:.4f}  "
              f"({sum(1 for c in report['cases'] if c.get('rr'))} / {s['cases_scored']})")
    print(f"  证据不足用例  : {s['empty_pass']} / {s['empty_cases']} 正确返回空")
    print()

    print("--- 分类指标 ---")
    for name, item in s["by_type"].items():
        print(f"  {name:22} n={item['count']:2}  "
              f"Recall={item['recall_at_k']:.3f}  命中率={item['hit_rate']:.3f}")
    print()

    warnings = [c for c in report["cases"] if c.get("warning")]
    if warnings:
        print("--- 标注待核对 ---")
        for w in warnings:
            print(f"  {w['id']}: {w['warning']}  query={w['query']!r}")
        print()

    failures = [
        c
        for c in report["cases"]
        if (c.get("recall") == 0) or (c.get("expected_empty") and not c.get("empty_ok"))
    ]
    if failures:
        print("--- 未命中用例 ---")
        for f in failures:
            if f.get("expected_empty"):
                print(f"  {f['id']:10} [应为空但返回 {f['returned']} 条]  {f['query']!r}")
            else:
                print(f"  {f['id']:10} [recall=0 相关={f.get('relevant_total')}]  {f['query']!r}")
        print()

    if args.show_cases:
        print("--- 全部用例 ---")
        for c in report["cases"]:
            mode = c["match_mode"]
            if c.get("expected_empty"):
                flag = "OK" if c["empty_ok"] else "FAIL"
                print(f"  {c['id']:10} {mode:14} {flag:5} returned={c['returned']}  {c['query']!r}")
            else:
                print(
                    f"  {c['id']:10} {mode:14} "
                    f"R={c.get('recall')} RR={c.get('rr')} "
                    f"found={c.get('relevant_found')}/{c.get('relevant_total')}  {c['query']!r}"
                )
        print()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入：{args.json}")

    print("=" * 72)
    print("提示：这是 BM25 词法基线。向量检索尚未接入，无法对比。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
