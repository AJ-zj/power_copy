"""端到端文案生成入口：研究 → 证据包 → 文案。

用法
----
    # 默认 Mock 模式（不产生任何费用）
    python scripts/generate_copy.py

    # 指定场景
    python scripts/generate_copy.py --topic "工商业企业参与电力市场前，需要了解哪些基本规则？"

    # 输出完整 JSON（证据包 + 文案）
    python scripts/generate_copy.py --json storage/copywriting/out.json

    # 真实 LLM 模式（需显式授权）
    python scripts/generate_copy.py --live --confirm-live

安全约束
--------
- 默认 Mock，不调用任何外部服务
- `--live` 需要同时满足：
  1. 显式传 `--confirm-live`
  2. 环境变量 `POWERCOPY_ALLOW_LIVE=1`
  3. `.env` 中配置了完整的 LLM 凭据
- 真实模式下整个工作流受 TaskBudget 约束（请求次数 + Token）
- 只把**公开政策原文**发给模型，不发送企业内部资料
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402

from powercopy.copywriting.agent import CopywritingAgent  # noqa: E402
from powercopy.copywriting.contracts import (  # noqa: E402
    CopywritingBudget,
    CopywritingRequest,
    GenerationMode,
    Platform,
)
from powercopy.copywriting.mock import MockCopywriterLLM  # noqa: E402
from powercopy.core.shared_budget import SharedBudget  # noqa: E402
from powercopy.research.agent import ResearchAgent  # noqa: E402
from powercopy.research.contracts import (  # noqa: E402
    ResearchBudget,
    ResearchRequest,
)
from powercopy.research.mock import MockResearchLLM  # noqa: E402
from powercopy.research.verification import PolicyCorpus  # noqa: E402
from powercopy.tools.builtin.policy_tools import build_live_registry  # noqa: E402

DEFAULT_DB = ROOT / "storage" / "policies_versioned.db"
DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"

DEFAULT_TOPIC = "工商业企业参与电力市场前，需要了解哪些基本规则？"
DEFAULT_AUDIENCE = "工商业用电企业"
DEFAULT_STYLE = "专业、通俗、可信"

LIVE_ENV = "POWERCOPY_ALLOW_LIVE"
REQUIRED_ENV = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")


def live_ready() -> tuple[bool, str]:
    """检查真实 LLM 是否可用。

    Returns:
        (ready, reason)
    """

    if os.environ.get(LIVE_ENV) != "1":
        return False, f"未设置环境变量 {LIVE_ENV}=1"

    settings = dotenv_values(ROOT / ".env")
    missing = [
        name
        for name in REQUIRED_ENV
        if not (os.environ.get(name) or settings.get(name))
    ]
    if missing:
        return False, f".env 缺少配置：{', '.join(missing)}"

    return True, ""


def print_research(package) -> None:
    stats = package.statistics()
    print("=" * 74)
    print("阶段一：政策研究")
    print("=" * 74)
    print(f"  research_id            : {package.research_id}")
    print(f"  research_status        : {package.research_status}")
    print(f"  status_reason          : {package.status_reason}")
    print(f"  问题                   : {len(package.research_questions)}")
    print(f"  证据条目               : {len(package.evidence_items)}")
    print(f"  原文核验通过的候选事实 : {stats['claims_text_verified']}")
    print(f"  其中语义待审核         : {stats['claims_pending_semantic_review']}")
    print(f"  人工确认通过           : {stats['claims_human_confirmed']}"
          f"（通道未实现，恒为 0）")
    print(f"  被拒绝                 : {stats['claims_rejected']}")
    print(f"  政策时效               : {package.policy_validity_status}")
    print()


def print_draft(draft) -> None:
    stats = draft.statistics()
    print("=" * 74)
    print("阶段二：文案草稿")
    print("=" * 74)
    print(f"  draft_id     : {draft.draft_id}")
    print(f"  status       : {draft.status}")
    print(f"  status_reason: {draft.status_reason}")
    print(f"  生成模式     : {draft.generation_mode}")
    print(f"  模型         : {draft.model_name or '（未指定）'}")
    print(f"  prompt 版本  : {draft.prompt_version}")
    print(f"  字数         : {stats['char_count']} / 目标 {draft.target_chars}")
    print(f"  事实要点     : {stats['key_points_total']}"
          f"（全部绑定证据，语义待审核 {stats['key_points_pending_semantic_review']}）")
    print(f"  人工确认     : {stats['key_points_human_confirmed']}"
          f"（通道未实现，恒为 0）")
    print(f"  引用证据     : {len(draft.evidence_refs)} 条")
    print(f"  Token        : {json.dumps(draft.token_usage, ensure_ascii=False)}")
    print()

    print("-" * 74)
    print(f"标题：{draft.title}")
    print("-" * 74)
    print()
    print(draft.body)
    print()

    if draft.key_points:
        print("-" * 74)
        print(f"事实要点与证据映射（{len(draft.key_points)}）")
        print("-" * 74)
        for point in draft.key_points:
            print(f"  {point.point_id}  [{point.semantic_status}]")
            print(f"    {point.text[:90]}")
            print(f"    证据：{point.evidence_ids}")
            if point.claim_ids:
                print(f"    claim：{point.claim_ids}")
            print()

    if draft.review_notes:
        print("-" * 74)
        print(f"待审核事项（{len(draft.review_notes)}）")
        print("-" * 74)
        for note in draft.review_notes:
            print(f"  ! {note}")
        print()

    print("=" * 74)
    print("声明：")
    print("  1. 本文案为程序生成的**待审核草稿**，不是可直接发布的内容。")
    print("  2. 每条事实要点已绑定真实证据 ID，但语义支持性未经确认。")
    print("  3. 政策时效与适用范围需人工核对官方公告。")
    print("=" * 74)


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端文案生成")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--audience", default=DEFAULT_AUDIENCE)
    parser.add_argument(
        "--platform",
        default="wechat_mp",
        choices=["wechat_mp", "website", "linkedin", "general"],
    )
    parser.add_argument("--target-chars", type=int, default=800)
    parser.add_argument("--style", default=DEFAULT_STYLE)
    parser.add_argument("--purpose", default="为工商业用电企业提供政策科普")
    parser.add_argument("--region", default="")

    parser.add_argument("--max-questions", type=int, default=5)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-api-attempts", type=int, default=6)
    parser.add_argument("--max-known-tokens", type=int, default=20_000)

    parser.add_argument("--json", type=Path, help="输出完整 JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)

    parser.add_argument(
        "--live", action="store_true", help="使用真实 LLM（需授权）"
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="确认本次使用真实 LLM（会产生费用）",
    )
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"政策库不存在：{args.db}", file=sys.stderr)
        return 2
    if not args.manifest.is_file():
        print(f"切片清单不存在：{args.manifest}", file=sys.stderr)
        return 2

    # ---- 模式判定 ----
    mode = GenerationMode.MOCK
    model_name = "mock"

    if args.live:
        if not args.confirm_live:
            print(
                "拒绝执行：使用真实 LLM 需要同时传 --confirm-live",
                file=sys.stderr,
            )
            return 2
        ready, reason = live_ready()
        if not ready:
            print(f"拒绝执行：{reason}", file=sys.stderr)
            return 2
        mode = GenerationMode.LIVE
        settings = dotenv_values(ROOT / ".env")
        model_name = str(
            os.environ.get("LLM_MODEL") or settings.get("LLM_MODEL") or ""
        )
        print("⚠ 真实 LLM 模式已启用，将产生 API 费用")
        print(f"  模型：{model_name}")
        print(f"  预算：最多 {args.max_api_attempts} 次请求，"
              f"{args.max_known_tokens} tokens")
        print()
    else:
        print("本地 Mock 模式（不调用任何外部服务，不产生费用）")
        print()

    print(f"政策库 : {args.db}")
    print(f"清单   : {args.manifest}")
    print()

    corpus = PolicyCorpus(args.db, args.manifest)
    registry = build_live_registry(args.db)

    # 两个阶段共用同一份预算。
    # 旧实现为研究阶段和文案阶段各建一个 TaskBudget（文案拿一半），
    # 导致整个任务的真实上限是两者之和，与用户设定不符。
    shared_budget = SharedBudget(
        max_api_attempts=args.max_api_attempts,
        max_known_tokens=args.max_known_tokens,
    )

    # ---- 阶段一：研究 ----
    research_request = ResearchRequest(
        topic=args.topic,
        target_audience=args.audience,
        region=args.region,
        research_objective=args.purpose,
        task_budget=ResearchBudget(
            max_questions=args.max_questions,
            max_tool_calls=args.max_tool_calls,
            max_api_attempts=args.max_api_attempts,
            max_known_tokens=args.max_known_tokens,
        ),
    )

    if mode is GenerationMode.LIVE:
        from powercopy.core.llm import LLMClient

        try:
            research_llm = LLMClient()
        except Exception as exc:
            print(f"真实模型不可用：{exc}", file=sys.stderr)
            return 2
    else:
        research_llm = MockResearchLLM(
            max_questions=args.max_questions
        )

    research_agent = ResearchAgent(
        research_llm,
        registry,
        corpus,
        budget=research_request.task_budget,
        shared_budget=shared_budget,
    )
    package = research_agent.run(research_request)
    print_research(package)

    # ---- 阶段二：文案 ----
    copy_request = CopywritingRequest(
        topic=args.topic,
        target_audience=args.audience,
        platform=Platform(args.platform),
        target_chars=args.target_chars,
        style=args.style,
        purpose=args.purpose,
        evidence_package=package,
        # 阶段字段只是契约占位，真正的约束是 shared_budget；
        # 不再额外减半（旧实现让文案阶段另拿一半，导致总上限翻倍）
        budget=CopywritingBudget(
            max_api_attempts=min(args.max_api_attempts, 20),
            max_known_tokens=min(args.max_known_tokens, 100_000),
        ),
    )

    if mode is GenerationMode.LIVE:
        try:
            copy_llm = LLMClient()
        except Exception as exc:
            print(f"真实模型不可用：{exc}", file=sys.stderr)
            return 2
    else:
        copy_llm = MockCopywriterLLM()

    copy_agent = CopywritingAgent(
        copy_llm, mode=mode, model_name=model_name
    )
    draft = copy_agent.run(copy_request)
    print_draft(draft)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "generation_mode": str(mode),
                    "evidence_package": package.model_dump(mode="json"),
                    "content_draft": draft.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n完整结果已写入：{args.json}")

    # 生成失败返回 1；pending_review / aborted 都算正常结束
    return 1 if str(draft.status) == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
