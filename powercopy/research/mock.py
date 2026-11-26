"""离线 Mock 研究规划器与提取器。

用途
----
- 端到端测试（不调用付费 LLM）
- 验收脚本演示
- CI 中稳定复现

设计
----
Mock 不做「智能」判断，只做**确定性的机械处理**：

- `MockPlanner`：把主题与目的按固定模板拆成问题
- `MockExtractor`：从检索片段中**逐字**截取一段作为 exact_quote

关键约束：Mock 生成的引文必须是原文的**精确子串**，
否则会被核验器判为 QUOTE_MISMATCH。这正是我们想验证的行为 ——
即使是 Mock，也必须遵守「引文必须真实存在」的规则。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from powercopy.research.agent import PAYLOAD_MARKER
from powercopy.research.contracts import compute_evidence_id

# 每个问题的检索词模板。刻意用不同角度，避免全部退化为同一查询。
_QUESTION_TEMPLATES = [
    ("经营主体准入条件", "经营主体 基本条件 注册"),
    ("市场注册流程与材料", "市场注册 流程 申请 承诺 审查 公示"),
    ("交易类型与交易方式", "交易类型 电能量交易 辅助服务"),
    ("信息披露要求", "信息披露 披露 公布"),
    ("违规处理与信用管理", "违规 处理 失信 管理"),
    ("政策适用范围与时效", "适用范围 施行 有效期"),
]


def _extract_payload(text: str, *, expect_list: bool = False):
    """从 PAYLOAD_MARKER 之后取 JSON。

    之所以需要标记，是因为 prompt 前半部分的「返回格式」里也含 `{` / `[`，
    直接找第一个括号会误命中格式说明。
    """

    if PAYLOAD_MARKER not in text:
        return [] if expect_list else {}

    tail = text.split(PAYLOAD_MARKER, 1)[1].strip()
    try:
        parsed = json.loads(tail)
    except json.JSONDecodeError:
        return [] if expect_list else {}

    if expect_list:
        return parsed if isinstance(parsed, list) else []
    return parsed if isinstance(parsed, dict) else {}


class MockPlanner:
    """确定性的问题拆解器。"""

    def __init__(self, max_questions: int = 6):
        self.max_questions = max_questions

    def plan(self, request) -> list[dict]:
        """按模板生成研究问题。

        会结合 topic 前缀，使问题读起来与主题相关。
        """

        topic = request.topic
        audience = request.target_audience or "工商业企业"
        region = request.region

        questions = []
        for label, keyword in _QUESTION_TEMPLATES[: self.max_questions]:
            if region:
                text = f"{region}地区{audience}参与电力市场的{label}是什么？"
            else:
                text = f"{audience}参与电力市场的{label}是什么？"
            questions.append({"text": text, "keyword": keyword})

        # 主题本身作为一个总览问题
        questions.insert(
            0,
            {
                "text": f"关于「{topic}」，政策有哪些基本规定？",
                "keyword": topic[:60],
            },
        )
        return questions[: self.max_questions]


class MockExtractor:
    """确定性的候选事实提取器。

    策略：对每个检索片段，截取开头一段连续原文作为引文，
    并用该片段的 evidence_id 作为引用。

    这不是「智能提取」，但足以验证：
    - 引文是否真实存在于原文
    - 引用链路是否完整
    - 无证据时是否正确返回空 claims

    相关性闸门
    ----------
    混合检索**必然返回 Top-K**，即使问题与语料完全无关。真实 LLM 的
    extractor prompt 要求「若片段不足以回答问题，返回空 claims」，
    但 Mock 没有语义判断能力。

    因此 Mock 提供一个**确定性的词面重叠检查**作为替代：
    问题与片段若无任何共享的 2 字以上子串，则视为不相关，不产出 claim。

    这**不是**语义相关性判断，只是为了在离线测试中能复现
    「检索候选不支持研究问题」的场景。真实链路由模型判断。
    """

    def __init__(
        self,
        *,
        quote_chars: int = 60,
        max_claims_per_question: int = 2,
        min_overlap_chars: int = 2,
    ):
        self.quote_chars = quote_chars
        self.max_claims_per_question = max_claims_per_question
        self.min_overlap_chars = min_overlap_chars

    def _is_relevant(self, question: str, text: str) -> bool:
        """词面重叠检查（Mock 专用，非语义判断）。"""

        if self.min_overlap_chars <= 0:
            return True

        # 去掉问题里的疑问词与标点，保留实词片段
        cleaned = "".join(
            ch for ch in question if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"
        )
        if not cleaned:
            return True

        size = self.min_overlap_chars
        for i in range(len(cleaned) - size + 1):
            if cleaned[i : i + size] in text:
                return True
        return False

    def extract(
        self, question: str, hits: list[dict], evidence_by_id: dict
    ) -> list[dict]:
        claims = []

        for hit in hits[: self.max_claims_per_question * 2]:
            if len(claims) >= self.max_claims_per_question:
                break

            text = str(hit.get("text") or "")
            if not text:
                continue

            evidence_id = compute_evidence_id(
                policy_id=str(hit["policy_id"]),
                version=int(hit["version"]),
                chunk_id=str(hit["chunk_id"]),
                start=int(hit["start"]),
                end=int(hit["end"]),
            )
            if evidence_id not in evidence_by_id:
                continue

            # 相关性闸门：片段与问题毫无字面交集时不产出 claim
            if not self._is_relevant(question, text):
                continue

            # 逐字截取原文开头，作为引文
            quote = text[: self.quote_chars]
            statement = self._make_statement(question, quote)

            claims.append(
                {
                    "statement": statement,
                    "evidence_ids": [evidence_id],
                }
            )

        return claims

    @staticmethod
    def _make_statement(question: str, quote: str) -> str:
        """把引文转成一句「候选事实」。

        刻意保持朴素，不做推断 —— 语义支持性留给人工审核。
        """

        clean = " ".join(quote.split())
        return f"就「{question}」，政策原文表述为：{clean}"


class MockResearchLLM:
    """包装 Mock 规划器/提取器，模拟 LLMClient 的 invoke 接口。

    使 ResearchAgent 可以用与真实 LLM 相同的调用路径运行。
    """

    def __init__(
        self,
        *,
        max_questions: int = 6,
        quote_chars: int = 60,
        fail_mode: str | None = None,
    ):
        self.planner = MockPlanner(max_questions=max_questions)
        self.extractor = MockExtractor(quote_chars=quote_chars)
        self.fail_mode = fail_mode
        self.calls: list[dict] = []

    def invoke(self, messages, model=None, **kwargs) -> dict:
        self.calls.append({"messages": messages, "model": model})

        if self.fail_mode == "invalid_json":
            return {"content": "这不是 JSON", "model": "mock"}
        if self.fail_mode == "empty":
            return {"content": "", "model": "mock"}

        system = ""
        user = ""
        for message in messages:
            if message.get("role") == "system":
                system = str(message.get("content") or "")
            elif message.get("role") == "user":
                user = str(message.get("content") or "")

        if "拆解" in system or "研究问题" in system and "拆解" in user:
            return {
                "content": self._plan_response(user),
                "model": "mock",
            }

        return {
            "content": self._extract_response(user),
            "model": "mock",
        }

    def _plan_response(self, user: str) -> str:
        """从 payload 标记后取 JSON，生成问题列表。"""

        payload = _extract_payload(user)
        topic = str(payload.get("topic") or "")
        audience = str(payload.get("target_audience") or "")
        region = str(payload.get("region") or "")

        request = SimpleNamespace(
            topic=topic, target_audience=audience, region=region
        )
        questions = self.planner.plan(request)
        return json.dumps({"questions": questions}, ensure_ascii=False)

    def _extract_response(self, user: str) -> str:
        """从 payload 标记后取 JSON 片段，逐字截取引文。"""

        snippets = _extract_payload(user, expect_list=True)
        if not snippets:
            return json.dumps(
                {"claims": [], "unanswered": "无法解析片段"},
                ensure_ascii=False,
            )

        # 从 prompt 头部取研究问题，用于相关性闸门
        question = ""
        for line in user.splitlines():
            if line.startswith("研究问题："):
                question = line[len("研究问题：") :].strip()
                break

        claims = []
        for snippet in snippets:
            text = str(snippet.get("text") or "")
            if not text:
                continue
            if question and not self.extractor._is_relevant(question, text):
                continue
            quote = text[: self.extractor.quote_chars]
            claims.append(
                {
                    "statement": f"政策原文表述为：{' '.join(quote.split())}",
                    "evidence_ids": [snippet.get("evidence_id")],
                }
            )

        return json.dumps(
            {
                "claims": claims,
                "unanswered": "" if claims else "检索片段与问题无字面交集",
            },
            ensure_ascii=False,
        )


__all__ = [
    "MockExtractor",
    "MockPlanner",
    "MockResearchLLM",
]
