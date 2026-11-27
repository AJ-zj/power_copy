"""统一业务执行入口：研究 → 文案 → 审核 → 修订。

用法
----
    # 默认 Mock（不产生费用），跑完整四阶段
    python scripts/run_content_workflow.py

    # 只跑 research → draft（不审核）
    python scripts/run_content_workflow.py --no-review

    # 指定主题与字数
    python scripts/run_content_workflow.py --topic "..." --target-chars 800

    # 输出完整 JSON
    python scripts/run_content_workflow.py --json storage/workflow/out.json

    # 真实 LLM（需显式授权）
    python scripts/run_content_workflow.py --live --confirm-live

安全约束
--------
- 默认 Mock，不调用任何外部服务
- `--live` 需要同时满足：`--confirm-live` + `POWERCOPY_ALLOW_LIVE=1`
  + `.env` 凭据齐全
- 整个流程共享一份预算
- 只发送公开政策原文，不发送企业内部资料
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

from powercopy.copywriting.contracts import GenerationMode  # noqa: E402
from powercopy.research.verification import PolicyCorpus  # noqa: E402
from powercopy.tools.builtin.policy_tools import build_live_registry  # noqa: E402
from powercopy.workflows import (  # noqa: E402
    ContentWorkflowRequest,
    ContentWorkflowService,
    WorkflowStatus,
)

DEFAULT_DB = ROOT / "storage" / "policies_versioned.db"
DEFAULT_MANIFEST = ROOT / "storage" / "rag" / "chunks.json"

LIVE_ENV = "POWERCOPY_ALLOW_LIVE"
REQUIRED_ENV = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")

STATUS_LABEL = {
    WorkflowStatus.DRAFT_READY: "草稿已生成（未审核）",
    WorkflowStatus.PENDING_REVIEW: "待人工审核",
    WorkflowStatus.NEEDS_MANUAL_REVIEW: "需人工处理",
    WorkflowStatus.ABORTED: "证据不足，已停止",
    WorkflowStatus.BUDGET_EXHAUSTED: "预算耗尽",
    WorkflowStatus.FAILED: "失败",
}


def live_ready() -> tuple[bool, str]:
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


def print_result(result) -> None:
    print("=" * 76)
    print("统一内容工作流结果")
    print("=" * 76)
    print(f"  task_id        : {result.task_id}")
    print(f"  topic          : {result.topic}")
    print(f"  mode           : {result.mode}")
    print(f"  最终状态       : {result.final_status}"
          f"（{STATUS_LABEL.get(result.final_status, '')}）")
    print(f"  状态说明       : {result.status_reason}")
    print(f"  需人工处理     : {result.requires_human()}")
    print()

    # ---- 阶段一 ----
    if result.research is not None:
        stats = result.research.statistics()
        print("-" * 76)
        print("阶段一：政策研究")
        print("-" * 76)
        print(f"  research_id            : {result.research.research_id}")
        print(f"  research_status        : {result.research.research_status}")
        print(f"  问题 / 证据            : "
              f"{len(result.research.research_questions)} / "
              f"{len(result.research.evidence_items)}")
        print(f"  原文核验通过的候选事实 : {stats['claims_text_verified']}")
        print(f"  其中语义待审核         : {stats['claims_pending_semantic_review']}")
        print(f"  人工确认通过           : {stats['claims_human_confirmed']}"
              f"（通道未实现，恒为 0）")
        print(f"  政策时效               : "
              f"{result.research.policy_validity_status}")
        print()

    # ---- 阶段二 ----
    if result.draft is not None:
        stats = result.draft.statistics()
        print("-" * 76)
        print("阶段二：文案草稿")
        print("-" * 76)
        print(f"  draft_id       : {result.draft.draft_id}")
        print(f"  status         : {result.draft.status}")
        print(f"  字数           : {stats['char_count']} / "
              f"目标 {result.draft.target_chars}")
        print(f"  事实要点       : {stats['key_points_total']}"
              f"（全部绑定证据，语义待审核 "
              f"{stats['key_points_pending_semantic_review']}）")
        print(f"  引用证据       : {len(result.draft.evidence_refs)} 条")
        print()
        print(f"  标题：{result.draft.title}")
        print()
        body_preview = result.draft.body
        if len(body_preview) > 900:
            body_preview = body_preview[:900] + "\n\n…（完整正文见 JSON 输出）"
        print(body_preview)
        print()

    # ---- 阶段三 ----
    if result.review is not None:
        print("-" * 76)
        print("阶段三：审核")
        print("-" * 76)
        review = result.review
        print(f"  review_id      : {review.review_id}")
        print(f"  status         : {review.status}")
        print(f"  说明           : {review.status_reason[:100]}")
        print(f"  事实支持性     : "
              f"{'已用 LLM 判断（机器意见）' if review.fact_support_llm_used else '未做语义审核'}")
        print(f"  人工确认通道   : "
              f"{'已执行' if review.human_confirmation_available else '未实现'}")
        print()
        print(f"  {'维度':<16} {'判定':<20} {'检查':>6} {'问题':>6}")
        print("  " + "-" * 56)
        for dim in review.dimension_results:
            print(f"  {str(dim.dimension):<16} {str(dim.verdict):<20} "
                  f"{dim.checked_items:>6} {dim.issue_count:>6}")
        print()
        if review.issues:
            print(f"  问题明细（{len(review.issues)}）：")
            for issue in review.issues[:10]:
                print(f"    [{issue.issue_id}] {issue.dimension}/{issue.severity} "
                      f"{issue.code}")
                print(f"        {issue.message[:90]}")
                if issue.auto_fixable:
                    print(f"        （可自动修订）")
            print()

    # ---- 阶段四 ----
    if result.revision is not None and result.revision.attempt > 0:
        print("-" * 76)
        print("阶段四：自动修订")
        print("-" * 76)
        rev = result.revision
        print(f"  revision_id    : {rev.revision_id}")
        print(f"  尝试次数       : {rev.attempt}（上限由请求指定）")
        print(f"  状态           : {rev.status}")
        print(f"  说明           : {rev.status_reason[:100]}")
        print(f"  内容有变化     : {rev.changed}")
        print(f"  已修正问题     : {rev.addressed_issue_ids}")
        print(f"  剩余问题       : {rev.remaining_issue_ids}")
        print(f"  修订后审核     : {rev.post_review_status}"
              f"（阻断级 {rev.post_review_blocking}）")
        print()

    # ---- 预算 ----
    print("-" * 76)
    print("预算快照")
    print("-" * 76)
    print(json.dumps(result.budget_snapshot, ensure_ascii=False, indent=2))
    print()
    print(f"  阶段耗时（ms）: {json.dumps(result.stage_timings_ms, ensure_ascii=False)}")
    print()

    print("=" * 76)
    print("声明：")
    print("  1. 本流程没有任何终态表示「可自动发布」。")
    print("  2. 所有产物默认待人工审核。")
    print("  3. 审核仅表示程序未发现已知硬性问题，不代表内容正确。")
    print("  4. 事实支持性若由 LLM 判断，属于机器意见，不代表专家确认。")
    print("  5. 政策是否现行有效程序无法判断，需核对官方公告。")
    print("=" * 76)


def main() -> int:
    parser = argparse.ArgumentParser(description="统一内容工作流")
    parser.add_argument(
        "--topic",
        default="工商业企业参与电力市场前，需要了解哪些基本规则？",
    )
    parser.add_argument("--audience", default="工商业用电企业")
    parser.add_argument("--region", default="")
    parser.add_argument(
        "--platform",
        default="wechat_mp",
        choices=["wechat_mp", "website", "linkedin", "general"],
    )
    parser.add_argument("--target-chars", type=int, default=800)
    parser.add_argument("--style", default="专业、通俗、可信")
    parser.add_argument("--purpose", default="为工商业用电企业提供政策科普")

    parser.add_argument("--no-review", action="store_true",
                        help="跳过审核阶段，只跑 research → draft")
    parser.add_argument("--no-revision", action="store_true",
                        help="审核失败时不自动修订")
    parser.add_argument("--max-revisions", type=int, default=1)

    parser.add_argument("--max-api-attempts", type=int, default=12)
    parser.add_argument("--max-known-tokens", type=int, default=40_000)
    parser.add_argument("--max-questions", type=int, default=5)
    parser.add_argument("--max-tool-calls", type=int, default=8)

    parser.add_argument("--json", type=Path, help="输出完整 JSON")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)

    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
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
    llm_factory = None

    if args.live:
        if not args.confirm_live:
            print("拒绝执行：真实 LLM 需要同时传 --confirm-live",
                  file=sys.stderr)
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
        print(f"  共享预算：最多 {args.max_api_attempts} 次请求，"
              f"{args.max_known_tokens} tokens（四阶段共用）")
        print()

        def llm_factory():  # noqa: F811
            from powercopy.core.llm import LLMClient

            return LLMClient()
    else:
        print("本地 Mock 模式（不调用任何外部服务，不产生费用）")
        print()

    print(f"政策库 : {args.db}")
    print(f"清单   : {args.manifest}")
    print()

    registry = build_live_registry(args.db)
    corpus = PolicyCorpus(args.db, args.manifest)

    service_kwargs = {"model_name": model_name}
    if llm_factory is not None:
        try:
            shared_client = llm_factory()
        except Exception as exc:
            print(f"真实模型不可用：{exc}", file=sys.stderr)
            return 2
        service_kwargs.update(
            research_llm=shared_client,
            copywriting_llm=shared_client,
            reviewer_llm=shared_client,
            reviser_llm=shared_client,
        )

    service = ContentWorkflowService(corpus, registry, **service_kwargs)

    request = ContentWorkflowRequest(
        topic=args.topic,
        target_audience=args.audience,
        region=args.region,
        platform=args.platform,
        target_chars=args.target_chars,
        style=args.style,
        purpose=args.purpose,
        mode=mode,
        enable_review=not args.no_review,
        enable_revision=not args.no_revision,
        max_revisions=args.max_revisions,
        max_api_attempts=args.max_api_attempts,
        max_known_tokens=args.max_known_tokens,
        max_questions=args.max_questions,
        max_tool_calls=args.max_tool_calls,
    )

    result = service.run(request)
    print_result(result)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result.model_dump(mode="json"),
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n完整结果已写入：{args.json}")

    # 失败才返回非零；其余都是正常的业务终态
    return 1 if result.final_status is WorkflowStatus.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
