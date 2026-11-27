"""政策研究与证据包验收脚本（离线，可独立运行）。

用法
----
    # 默认场景：真实政策库 + Mock LLM
    python scripts/run_research.py

    # 证据不足场景（知识库没有收录的地方性政策）
    python scripts/run_research.py --scenario insufficient

    # 自定义主题
    python scripts/run_research.py --topic "江西售电公司准入条件"

    # 输出完整 JSON 证据包
    python scripts/run_research.py --json storage/research/out.json

    # 指定用户问题
    python scripts/run_research.py --question "注册需要哪些材料" --question "违规如何处罚"

说明
----
完全离线：
- 不调用付费 LLM（使用 Mock 规划器/提取器）
- 不发起网络请求
- 只读打开本地政策库

证据核验是真实的：Mock 生成的引文同样要通过
`EvidenceVerifier` 的偏移/引文/哈希校验。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from powercopy.research.agent import ResearchAgent  # noqa: E402
from powercopy.research.contracts import (  # noqa: E402
    ResearchBudget,
    ResearchRequest,
    ResearchStatus,
)
from powercopy.research.mock import MockResearchLLM  # noqa: E402
from powercopy.research.verification import PolicyCorpus  # noqa: E402
from powercopy.tools.builtin.policy_tools import build_live_registry  # noqa: E402

DEFAULT_DB = ROOT / "storage" / "policies_versioned.db"
DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"

SCENARIOS = {
    "default": {
        "topic": "工商业企业参与电力市场前，需要了解哪些基本规则？",
        "target_audience": "工商业企业",
        "region": "",
        "research_objective": "为售电业务的政策研究与文案撰写提供可追溯依据",
    },
    "insufficient": {
        "topic": "江西省售电公司参与电力现货市场的交易规则是什么？",
        "target_audience": "江西省售电公司",
        "region": "江西",
        "research_objective": "核实知识库中是否存在江西省地方性交易政策",
    },
}


def print_package(pkg, *, verbose: bool) -> None:
    print("=" * 74)
    print("研究证据包")
    print("=" * 74)
    print(f"  research_id      : {pkg.research_id}")
    print(f"  topic            : {pkg.topic}")
    print(f"  region           : {pkg.region or '（未指定）'}")
    print(f"  target_audience  : {pkg.target_audience or '（未指定）'}")
    print(f"  research_status  : {pkg.research_status}")
    print(f"  status_reason    : {pkg.status_reason}")
    print()
    print(f"  政策时效状态     : {pkg.policy_validity_status}")
    print(f"  时效说明         : {pkg.policy_validity_note}")
    print()

    print("-" * 74)
    print(f"研究问题（{len(pkg.research_questions)}）")
    print("-" * 74)
    for question in pkg.research_questions:
        mark = "✓" if question.answered else "✗"
        print(f"  [{mark}] {question.question_id}  {question.text}")
    print()

    print("-" * 74)
    print(
        f"候选事实（{len(pkg.claims)}，其中待人工确认语义支持性 "
        f"{pkg.verified_claim_count()}）"
    )
    print("-" * 74)
    for claim in pkg.claims:
        print(f"  {claim.claim_id}  [{claim.status}]")
        print(f"    陈述：{claim.statement[:100]}")
        print(f"    证据：{claim.evidence_ids}")
        if verbose and claim.support_note:
            print(f"    备注：{claim.support_note}")
    print()

    print("-" * 74)
    print(f"证据条目（{len(pkg.evidence_items)}）")
    print("-" * 74)
    for item in pkg.evidence_items:
        print(f"  {item.evidence_id}  [{item.status}]")
        print(f"    {item.title or '（无标题）'}")
        print(
            f"    policy={item.policy_id[:12]} v{item.version} "
            f"chunk={item.chunk_id[:12]}"
        )
        print(f"    区间 [{item.start}, {item.end})  页={item.page}")
        print(f"    哈希 {item.content_sha256[:16]}...")
        print(f"    来源 {item.source_url or '（无）'}")
        if verbose:
            print(f"    引文 {item.exact_quote[:80]!r}")
        print()

    if pkg.unanswered_questions:
        print("-" * 74)
        print(f"未解决问题（{len(pkg.unanswered_questions)}）")
        print("-" * 74)
        for text in pkg.unanswered_questions:
            print(f"  ✗ {text}")
        print()

    if pkg.uncertainties:
        print("-" * 74)
        print(f"不确定项（{len(pkg.uncertainties)}）")
        print("-" * 74)
        for text in pkg.uncertainties:
            print(f"  ! {text}")
        print()

    if pkg.verification_issues:
        print("-" * 74)
        print(f"核验问题（{len(pkg.verification_issues)}）")
        print("-" * 74)
        for issue in pkg.verification_issues:
            print(
                f"  [{issue.code}] {issue.scope}:{issue.target_id} "
                f"{issue.detail[:80]}"
            )
        print()

    print("=" * 74)
    print("声明：")
    print("  1. 上列为检索候选与原文核验结果，不是已核实的业务结论。")
    print("  2. 原文匹配成功 ≠ 事实在语义上成立，语义支持性需人工确认。")
    print("  3. 引用的版本是本地库最新观测版本，不代表政策现行有效。")
    print("=" * 74)


def main() -> int:
    parser = argparse.ArgumentParser(description="政策研究与证据包")
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="default",
        help="预设场景",
    )
    parser.add_argument("--topic", help="覆盖研究主题")
    parser.add_argument("--audience", default="", help="目标客户")
    parser.add_argument("--region", default="", help="目标地区")
    parser.add_argument("--objective", default="", help="研究目的")
    parser.add_argument(
        "--question",
        action="append",
        default=[],
        help="指定研究问题，可重复",
    )
    parser.add_argument("--max-questions", type=int, default=4)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--json", type=Path, help="输出完整 JSON 证据包")
    parser.add_argument("--verbose", action="store_true", help="输出引文全文")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"政策库不存在：{args.db}", file=sys.stderr)
        return 2
    if not args.manifest.is_file():
        print(f"切片清单不存在：{args.manifest}", file=sys.stderr)
        print(
            "请先运行：python scripts/build_chunks.py "
            f"--db {args.db} --out {args.manifest}",
            file=sys.stderr,
        )
        return 2

    scenario = SCENARIOS[args.scenario]
    topic = args.topic or scenario["topic"]

    request = ResearchRequest(
        topic=topic,
        target_audience=args.audience or scenario["target_audience"],
        region=args.region or scenario["region"],
        research_objective=args.objective or scenario["research_objective"],
        required_questions=args.question,
        task_budget=ResearchBudget(
            max_questions=args.max_questions,
            max_tool_calls=args.max_tool_calls,
        ),
    )

    print(f"政策库 : {args.db}")
    print(f"清单   : {args.manifest}")
    print(f"场景   : {args.scenario}")
    print()

    corpus = PolicyCorpus(args.db, args.manifest)
    registry = build_live_registry(args.db)

    llm = MockResearchLLM(max_questions=request.task_budget.max_questions)
    agent = ResearchAgent(
        llm, registry, corpus, budget=request.task_budget
    )

    package = agent.run(request)
    print_package(package, verbose=args.verbose)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                package.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n完整证据包已写入：{args.json}")

    # 退出码：completed → 0；partial/failed → 0（研究结果，非程序错误）
    # 只有契约或环境错误才返回非零
    return 0 if package.research_status in (
        ResearchStatus.COMPLETED,
        ResearchStatus.PARTIAL,
        ResearchStatus.FAILED,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
