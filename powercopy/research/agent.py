"""Research Agent：有限研究流程。

职责边界
--------
**模型负责**（语义工作）：
- 拆解研究问题
- 决定检索什么
- 判断检索结果是否相关
- 提出候选事实并指定引用

**程序负责**（可靠性）：
- 预算强制
- 数据契约校验
- 证据 ID 生成与核验
- 失败状态判定
- 不把候选当事实

流程
----
1. 拆解研究问题（模型）
2. 对每个问题调用检索工具（模型决策，程序执行）
3. 必要时回查原文（get_policy_detail）
4. 汇总候选证据 + 候选事实（模型）
5. 程序核验证据与引用
6. 输出证据包

为什么不用 Agent 的自动 tool loop
--------------------------------
`powercopy.core.agent.Agent` 是通用 tool-calling 循环，适合开放式任务。
本研究需要**固定的阶段顺序**与**阶段间的程序校验**（例如证据必须先核验
才能被 claim 引用）。用 Agent 的开放式循环无法保证这一点。

因此本模块**复用** Agent 的构件（LLMClient / ToolRegistry / ToolExecutor /
TaskBudget / Trace），但自己编排阶段。这不是「新 Agent 框架」，而是
在既有构件上的一次受限编排。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powercopy.core.budget import BudgetExceeded as CoreBudgetExceeded
from powercopy.core.budget import TaskBudget
from powercopy.core.trace import ExecutionTrace, get_current_trace
from powercopy.research.contracts import (
    MAX_CLAIMS,
    Claim,
    ClaimStatus,
    EvidenceItem,
    EvidencePackage,
    EvidenceStatus,
    PolicyValidityStatus,
    ResearchBudget,
    ResearchQuestion,
    ResearchRequest,
    ResearchStatus,
    compute_evidence_id,
    utc_now_iso,
)
from powercopy.research.verification import (
    EvidenceVerifier,
    PolicyCorpus,
    VerificationReport,
    build_package,
)
from powercopy.tools.executor import ToolExecutor
from powercopy.tools.registry import ToolRegistry

# ---------------------------------------------------------------- 系统提示

PLANNER_PROMPT = """你是电力市场政策研究助手，负责把研究主题拆解为可检索的研究问题。

要求：
1. 只依据研究主题、目标客户、地区与研究目的拆解
2. 每个问题应当能通过检索政策原文来回答
3. 问题要具体，避免「政策有哪些」这类过于宽泛的表述
4. 不要臆造政策内容，不要给出结论
5. 只返回 JSON"""

EXTRACTOR_PROMPT = """你是电力市场政策研究助手，负责从检索到的政策原文片段中提取候选事实。

严格要求：
1. 每条事实必须引用给定的 evidence_id，不得编造 ID
2. exact_quote 必须逐字复制原文片段中的连续文本，不得改写、拼接或省略
3. 只提取检索片段中明确写出的内容，不做推断
4. 若片段不足以回答问题，返回空的 claims 并在 unanswered 中说明
5. 不要判断政策是否现行有效
6. 只返回 JSON"""

# 结构化输入的分隔标记。
# 用途：让模型（以及 Mock）能明确区分「指令」与「数据」，
# 避免把格式说明里的 `{` / `[` 误当成数据起始。
PAYLOAD_MARKER = "<<<PAYLOAD_JSON>>>"
RESULT_MARKER = "<<<RESULT_JSON>>>"


@dataclass
class ResearchContext:
    """研究过程的中间状态。"""

    request: ResearchRequest
    research_id: str
    trace: ExecutionTrace | None = None
    tool_calls: int = 0
    budget: TaskBudget | None = None
    """统一的模型调用预算。真实 LLM 与内部重试都受它约束。"""


class ResearchError(RuntimeError):
    """研究流程无法继续。"""


class BudgetExhausted(ResearchError):
    """任务预算耗尽。"""


class ResearchAgent:
    """受限研究流程的编排器。

    与 `powercopy.core.agent.Agent` 的区别：
    - Agent 是通用 tool-calling 循环
    - 本类固定阶段顺序，阶段之间做程序校验
    """

    def __init__(
        self,
        llm: Any,
        registry: ToolRegistry,
        corpus: PolicyCorpus,
        *,
        budget: ResearchBudget | None = None,
        planner: Any = None,
        extractor: Any = None,
        shared_budget: Any = None,
    ):
        """
        Args:
            llm: 规划与提取使用的模型客户端（可为 Mock）
            registry: 工具注册表（含 search_policy_evidence 等）
            corpus: 本地政策库与切片清单
            budget: 阶段预算上限（max_questions / max_tool_calls 等）
            planner: 可选的规划器替身（测试用）
            extractor: 可选的提取器替身（测试用）
            shared_budget: 跨阶段共享的预算（SharedBudget）。
                传入时**以它为准**，不再另建 TaskBudget ——
                这样研究、生成、审核共享同一份剩余额度。
        """

        self.llm = llm
        self.registry = registry
        self.corpus = corpus
        self.budget = budget or ResearchBudget()
        self.executor = ToolExecutor(registry)
        self.verifier = EvidenceVerifier(corpus)
        self.planner = planner
        self.extractor = extractor
        self.shared_budget = shared_budget

    # ---- 主流程 ----

    def run(self, request: ResearchRequest) -> EvidencePackage:
        """执行一次研究，返回证据包。

        任何阶段失败都不抛异常（除非预算耗尽或契约错误），
        而是产出带明确状态的证据包 —— 研究失败也是研究结果。
        """

        research_id = f"res-{uuid.uuid4().hex[:16]}"
        trace = get_current_trace()

        # 统一预算：真实 LLM 调用与内部重试都受 TaskBudget 约束。
        # 之前只用一个自增的 tool_calls 计数器管工具，模型调用完全没有预算，
        # 真实 LLM 模式下可能无限调用。
        #
        # 若外部传入了 shared_budget（跨阶段共享），直接复用它 ——
        # 否则新建一个阶段内预算。
        core_budget = self.shared_budget
        if core_budget is None:
            core_budget = TaskBudget(
                max_api_attempts=self.budget.max_api_attempts,
                max_known_tokens=self.budget.max_known_tokens,
            )

        ctx = ResearchContext(
            request=request,
            research_id=research_id,
            trace=trace,
            budget=core_budget,
        )

        uncertainties: list[str] = []
        issues_report = VerificationReport()

        try:
            questions = self._plan_questions(ctx, uncertainties)
        except BudgetExhausted:
            return self._failed_package(
                ctx, "预算耗尽，无法完成问题拆解"
            )
        except Exception as exc:
            return self._failed_package(ctx, f"问题拆解失败：{exc}")

        if not questions:
            return self._failed_package(ctx, "未能拆解出可检索的研究问题")

        # 逐问题检索 + 提取
        evidence_by_id: dict[str, EvidenceItem] = {}
        raw_claims: list[Claim] = []
        answered_questions: list[ResearchQuestion] = []
        unanswered: list[str] = []

        for question in questions:
            if ctx.tool_calls >= self.budget.max_tool_calls:
                unanswered.append(question.text)
                uncertainties.append(
                    f"工具调用预算耗尽，问题「{question.text}」未检索"
                )
                answered_questions.append(
                    question.model_copy(update={"answered": False})
                )
                continue

            hits = self._search(ctx, question.text, uncertainties)

            if not hits:
                unanswered.append(question.text)
                answered_questions.append(
                    question.model_copy(update={"answered": False})
                )
                uncertainties.append(
                    f"问题「{question.text}」未检索到任何候选片段；"
                    f"这不等于政策中不存在相关规定"
                )
                continue

            new_evidence = self._candidates_to_evidence(
                hits, issues_report
            )
            for item in new_evidence:
                evidence_by_id.setdefault(item.evidence_id, item)

            try:
                claims, done = self._extract(
                    ctx, question, hits, evidence_by_id, uncertainties
                )
            except BudgetExhausted:
                # 预算耗尽：保留已检索到的证据，剩余问题记为未解决，
                # 继续走核验与汇总流程，产出 PARTIAL 证据包。
                unanswered.append(question.text)
                answered_questions.append(
                    question.model_copy(update={"answered": False})
                )
                uncertainties.append(
                    f"模型预算耗尽，问题「{question.text}」未提取候选事实；"
                    f"已检索到的原文片段仍作为候选证据保留"
                )
                for remaining in questions[questions.index(question) + 1 :]:
                    if remaining.text not in unanswered:
                        unanswered.append(remaining.text)
                        answered_questions.append(
                            remaining.model_copy(update={"answered": False})
                        )
                        uncertainties.append(
                            f"模型预算耗尽，问题「{remaining.text}」未检索"
                        )
                break
            except ResearchError as exc:
                unanswered.append(question.text)
                answered_questions.append(
                    question.model_copy(update={"answered": False})
                )
                uncertainties.append(
                    f"问题「{question.text}」提取候选事实失败：{exc}"
                )
                continue

            raw_claims.extend(claims)

            if done and claims:
                answered_questions.append(
                    question.model_copy(
                        update={
                            "answered": True,
                            "answer": claims[0].statement,
                            "claim_ids": [c.claim_id for c in claims],
                        }
                    )
                )
            else:
                unanswered.append(question.text)
                answered_questions.append(
                    question.model_copy(update={"answered": False})
                )

        # 核验证据
        verified: dict[str, EvidenceItem] = {}
        for item in list(evidence_by_id.values())[:MAX_CLAIMS]:
            out = self.verifier.verify_evidence(item, issues_report)
            if out is not None:
                verified[out.evidence_id] = out

        # 核验 claim 引用
        final_claims = self.verifier.verify_claims(
            raw_claims[:MAX_CLAIMS], verified, issues_report
        )

        # ---- 依据最终核验结果重算问题完成状态 ----
        #
        # 之前的问题状态是在「提取完 claim」时就定下的，那时还没做核验。
        # 若某条 claim 因引用无效证据被拒，对应问题本应退回未解决，
        # 但旧实现仍标为 answered —— 下游会误以为该问题已解决。
        #
        # 现在改为：以已通过核验的 claim（PENDING_REVIEW）为准重算。
        usable_claim_ids = {
            c.claim_id
            for c in final_claims
            if c.status is ClaimStatus.PENDING_REVIEW
        }
        recomputed: list[ResearchQuestion] = []
        reverted_not_answered = 0

        for question in answered_questions:
            surviving = [
                cid for cid in question.claim_ids if cid in usable_claim_ids
            ]
            if question.answered and not surviving:
                # 原本「已回答」，但其全部 claim 已被拒 → 退回未解决
                reverted_not_answered += 1
                recomputed.append(
                    question.model_copy(
                        update={"answered": False, "claim_ids": []}
                    )
                )
                if question.text not in unanswered:
                    unanswered.append(question.text)
                continue
            recomputed.append(
                question.model_copy(update={"claim_ids": surviving})
            )

        answered_questions = recomputed

        if reverted_not_answered:
            uncertainties.append(
                f"{reverted_not_answered} 个问题因其引用的候选事实未通过核验，"
                f"已退回未解决状态"
            )

        # 时效
        validity, validity_note = self.verifier.assess_validity(
            list(verified.values())
        )

        # 地区适用性：未指定地区 → 不评判；指定了但证据无法确认 → 必须标注
        region_check = self.verifier.assess_region_applicability(
            request.region, list(verified.values())
        )

        if region_check["status"] not in ("no_region_requested", "matched"):
            uncertainties.append(
                f"地区适用性无法确认：{region_check['detail']}"
            )

        # 地区不适用时，已「回答」的问题必须退回未解决状态。
        #
        # 原因：证据本身真实存在（claim 保留），但它们是国家级政策原文，
        # 无法支撑「江西地方交易规则」这类问题。把问题标为已解决会误导下游。
        if not region_check["applicable"]:
            reverted = 0
            rebuilt: list[ResearchQuestion] = []
            for question in answered_questions:
                if question.answered:
                    reverted += 1
                    rebuilt.append(
                        question.model_copy(update={"answered": False})
                    )
                    if question.text not in unanswered:
                        unanswered.append(question.text)
                else:
                    rebuilt.append(question)
            answered_questions = rebuilt
            if reverted:
                uncertainties.append(
                    f"因地区适用性无法确认，{reverted} 个问题已退回未解决状态；"
                    f"相关原文片段仍作为候选证据保留，"
                    f"但不足以回答指定地区的问题"
                )

        # 完成度
        status, status_reason = self.verifier.decide_status(
            questions=answered_questions,
            claims=final_claims,
            unanswered=unanswered,
            report=issues_report,
        )

        # 地区不适用时不得宣称研究完成
        if not region_check["applicable"] and status is ResearchStatus.COMPLETED:
            status = ResearchStatus.PARTIAL
            status_reason = "证据无法确认适用于指定地区；" + status_reason

        if not verified:
            uncertainties.append(
                "没有任何证据通过程序核验，本报告不含可引用的事实依据"
            )

        return build_package(
            research_id=research_id,
            request=request,
            questions=answered_questions,
            claims=final_claims,
            evidence=list(verified.values()),
            unanswered=unanswered,
            uncertainties=uncertainties,
            report=issues_report,
            trace_reference=self._trace_ref(trace),
            status=status,
            status_reason=status_reason,
            validity=validity,
            validity_note=validity_note,
        )

    # ---- 阶段 1：拆解 ----

    def _plan_questions(
        self, ctx: ResearchContext, uncertainties: list[str]
    ) -> list[ResearchQuestion]:
        request = ctx.request

        # 用户已指定问题 → 直接用，模型仍可补充（但受预算约束）
        if request.required_questions:
            questions = [
                ResearchQuestion(
                    question_id=f"q{i+1}", text=text
                )
                for i, text in enumerate(request.required_questions)
            ]
            if len(questions) > self.budget.max_questions:
                uncertainties.append(
                    f"用户提供 {len(questions)} 个问题，超出预算 "
                    f"{self.budget.max_questions}，仅处理前 "
                    f"{self.budget.max_questions} 个"
                )
                questions = questions[: self.budget.max_questions]
            return questions

        if self.planner is not None:
            raw = self.planner.plan(request)
        else:
            raw = self._call_planner(ctx)

        questions = []
        for index, item in enumerate(raw[: self.budget.max_questions]):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            questions.append(
                ResearchQuestion(question_id=f"q{index+1}", text=text[:300])
            )

        if not questions:
            uncertainties.append("模型未能给出可用的研究问题")
        return questions

    def _call_planner(self, ctx: ResearchContext) -> list[dict]:
        request = ctx.request
        payload = {
            "topic": request.topic,
            "target_audience": request.target_audience,
            "region": request.region,
            "research_objective": request.research_objective,
            "max_questions": self.budget.max_questions,
        }
        prompt = (
            "请把以下研究主题拆解为不超过 "
            f"{self.budget.max_questions} 个可检索的研究问题。\n"
            '返回格式：{"questions": [{"text": "..."}]}\n\n'
            + PAYLOAD_MARKER
            + "\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

        content = self._invoke_json(ctx, PLANNER_PROMPT, prompt)
        return list(content.get("questions") or [])

    # ---- 阶段 2：检索 ----

    def _search(
        self, ctx: ResearchContext, query: str, uncertainties: list[str]
    ) -> list[dict]:
        if ctx.tool_calls >= self.budget.max_tool_calls:
            raise BudgetExhausted("工具调用预算耗尽")

        ctx.tool_calls += 1
        result = self.executor.execute(
            "search_policy_evidence",
            json.dumps(
                {"query": query, "top_k": self.budget.max_evidence_per_question},
                ensure_ascii=False,
            ),
        )

        if str(result.execution_status) != "ok":
            uncertainties.append(
                f"检索工具执行失败（{result.execution_status}）："
                f"{result.error_message or '未知原因'}"
            )
            return []

        if str(result.business_status) == "failed":
            uncertainties.append(
                f"检索不可用：{result.error_message or result.business_code}"
            )
            return []

        data = result.data if isinstance(result.data, dict) else {}
        if data.get("index_freshness") not in (None, "ok"):
            uncertainties.append(
                f"索引状态异常：{data.get('index_freshness')} — "
                f"{data.get('index_freshness_detail', '')}"
            )
        if data.get("degraded_status"):
            uncertainties.append(
                f"检索处于降级模式：{data['degraded_status']}"
            )

        return list(data.get("evidence_candidates") or [])

    # ---- 阶段 3：候选转证据 ----

    def _candidates_to_evidence(
        self, hits: list[dict], report: VerificationReport
    ) -> list[EvidenceItem]:
        """把检索候选转换为证据条目。

        候选携带的元信息（policy_id / version / start / end 等）来自索引，
        本身可信度中等 —— 真正的核验在 `verify_evidence` 阶段做。
        这里只做契约构造，任何不合契约的候选直接丢弃并记录。
        """

        items: list[EvidenceItem] = []

        for hit in hits:
            try:
                start = int(hit["start"])
                end = int(hit["end"])
                quote = str(hit["text"])[: end - start] if end > start else ""
                # 候选可能被截断（text_truncated），截断的不作为证据
                if hit.get("text_truncated"):
                    report.add(
                        "evidence",
                        "TRUNCATED_CANDIDATE",
                        str(hit.get("chunk_id", ""))[:16],
                        "候选正文被截断，无法作为完整引文",
                    )
                    continue
                if not quote:
                    continue
                evidence_id = compute_evidence_id(
                    policy_id=str(hit["policy_id"]),
                    version=int(hit["version"]),
                    chunk_id=str(hit["chunk_id"]),
                    start=start,
                    end=end,
                )
                items.append(
                    EvidenceItem(
                        evidence_id=evidence_id,
                        policy_id=str(hit["policy_id"]),
                        document_id=str(hit["document_id"]),
                        version=int(hit["version"]),
                        chunk_id=str(hit["chunk_id"]),
                        source_url=str(hit.get("source_url") or "")[:500],
                        title=str(hit.get("title") or "")[:300],
                        exact_quote=quote[:2_000],
                        start=start,
                        end=end,
                        page=hit.get("page"),
                        content_sha256=str(hit.get("content_sha256") or ""),
                        status=EvidenceStatus.CANDIDATE,
                    )
                )
            except Exception as exc:
                report.add(
                    "evidence",
                    "CANDIDATE_INVALID",
                    str(hit.get("chunk_id", ""))[:16],
                    f"候选无法构造为证据：{exc}",
                )

        return items

    # ---- 阶段 4：提取候选事实 ----

    def _extract(
        self,
        ctx: ResearchContext,
        question: ResearchQuestion,
        hits: list[dict],
        evidence_by_id: dict[str, EvidenceItem],
        uncertainties: list[str],
    ) -> tuple[list[Claim], bool]:
        if self.extractor is not None:
            raw = self.extractor.extract(question.text, hits, evidence_by_id)
        else:
            raw = self._call_extractor(ctx, question.text, hits)

        claims: list[Claim] = []
        for index, item in enumerate(raw):
            try:
                statement = str(item.get("statement") or "").strip()
                ids = [str(x) for x in (item.get("evidence_ids") or [])]
                if not statement or not ids:
                    continue
                # 只保留真实存在的 evidence_id
                valid = [i for i in ids if i in evidence_by_id]
                if not valid:
                    uncertainties.append(
                        f"问题「{question.text}」的候选事实引用了不存在的证据，已丢弃"
                    )
                    continue
                claims.append(
                    Claim(
                        claim_id=f"c-{question.question_id}-{index+1}",
                        question_id=question.question_id,
                        statement=statement[:1_000],
                        evidence_ids=valid,
                        status=ClaimStatus.CANDIDATE,
                        support_note="",
                    )
                )
            except Exception:
                uncertainties.append(
                    f"问题「{question.text}」的候选事实格式不合契约，已丢弃"
                )

        return claims, bool(claims)

    def _call_extractor(
        self, ctx: ResearchContext, question: str, hits: list[dict]
    ) -> list[dict]:
        # 只给模型必要的最小信息：evidence_id + 原文
        snippets = [
            {
                "evidence_id": compute_evidence_id(
                    policy_id=str(h["policy_id"]),
                    version=int(h["version"]),
                    chunk_id=str(h["chunk_id"]),
                    start=int(h["start"]),
                    end=int(h["end"]),
                ),
                "title": h.get("title"),
                "text": h.get("text"),
            }
            for h in hits
        ]
        prompt = (
            f"研究问题：{question}\n\n"
            "以下是检索到的政策原文片段。请提取能回答该问题的候选事实。\n"
            '返回格式：{"claims": [{"statement": "...", "evidence_ids": ["..."]}], '
            '"unanswered": "若无法回答，说明原因"}\n\n'
            + PAYLOAD_MARKER
            + "\n"
            + json.dumps(snippets, ensure_ascii=False, indent=2)
        )
        content = self._invoke_json(ctx, EXTRACTOR_PROMPT, prompt)
        return list(content.get("claims") or [])

    # ---- 模型调用 ----

    def _invoke_json(
        self, ctx: ResearchContext, system: str, prompt: str
    ) -> dict:
        """调用模型并解析 JSON。受 TaskBudget 约束。

        Args:
            ctx: 研究上下文，其 budget 是统一预算

        Raises:
            BudgetExhausted: 预算耗尽
            ResearchError: 输出为空或无法解析
        """

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        budget = ctx.budget
        if budget is not None:
            # 只做「是否已耗尽」的前置检查；预占与 usage 累计由
            # LLMClient._execute 内部完成，避免重复计数。
            stop = budget.stop_reason()
            if stop is not None:
                raise BudgetExhausted(f"模型预算耗尽（{stop}）")

        try:
            # 关键：把 budget 传给 LLMClient，让内部每次真实请求
            # （含重试）都受同一预算约束。旧实现在外部 reserve 后
            # 不传 budget，导致内部重试完全不受控。
            response = self.llm.invoke(messages, budget=budget)
        except CoreBudgetExceeded as exc:
            if budget is not None and hasattr(budget, "_exhausted_by"):
                pass
            raise BudgetExhausted(
                f"模型预算耗尽（{getattr(exc, 'reason', 'unknown')}）"
            ) from exc
        except Exception as exc:
            raise ResearchError(f"模型调用失败：{exc}") from exc

        raw = response.get("content") if isinstance(response, dict) else None
        if not raw:
            raise ResearchError("模型返回空内容")

        text = str(raw).strip()
        # 容忍 ```json 包裹
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResearchError(f"模型结构化输出无法解析：{exc}") from exc

        if not isinstance(parsed, dict):
            raise ResearchError("模型结构化输出不是 JSON 对象")
        return parsed

    # ---- 工具 ----

    def _trace_ref(self, trace: ExecutionTrace | None) -> dict:
        if trace is None:
            return {}
        try:
            snapshot = trace.snapshot()
        except Exception:
            return {}
        return {
            "task_id": snapshot.get("task_id"),
            "status": snapshot.get("status"),
            "event_count": len(snapshot.get("events") or []),
        }

    def _failed_package(
        self, ctx: ResearchContext, reason: str
    ) -> EvidencePackage:
        return build_package(
            research_id=ctx.research_id,
            request=ctx.request,
            questions=[],
            claims=[],
            evidence=[],
            unanswered=[ctx.request.topic],
            uncertainties=[reason],
            report=VerificationReport(),
            trace_reference=self._trace_ref(ctx.trace),
            status=ResearchStatus.FAILED,
            status_reason=reason,
            validity=PolicyValidityStatus.UNKNOWN,
            validity_note="研究未完成，无法评估政策时效",
        )


__all__ = [
    "BudgetExhausted",
    "EXTRACTOR_PROMPT",
    "PLANNER_PROMPT",
    "ResearchAgent",
    "ResearchContext",
    "ResearchError",
]
