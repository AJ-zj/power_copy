"""离线 Mock 文案生成器。

用途
----
- 端到端测试（不调用付费 LLM）
- 验收脚本演示
- CI 中稳定复现

设计
----
Mock **不做语义创作**，只做确定性的模板填充：

1. 从证据包中挑出**可用证据**（未被拒绝的 claim 引用的）
2. 每条证据写一段「政策原文表述为……」的要点
3. 拼成一篇结构完整的说明文
4. 所有 key_points 都绑定真实 evidence_id

为什么这样设计
--------------
Mock 的价值在于验证**流程与约束**，不在于文案质量：

- 引用校验能否通过（因为它引用的是真实 ID）
- 被拒的 claim 是否真的被排除
- 禁止内容检测是否生效（Mock 输出天然不含报价/承诺）
- 证据不足时能否正确 aborted

因此 Mock 输出的文案是朴素的、机械的，但**每一条都可追溯**。
"""

from __future__ import annotations

import json
from typing import Any

from powercopy.research.contracts import (
    ClaimStatus,
    EvidencePackage,
)


def _pick_sentence(quote: str, limit: int) -> str:
    """从原文片段中挑一个**完整的陈述句**。

    切片是按字符区间硬切的，片段首尾都可能是半句（例如「营主体包括…」
    这种从词中间开始的残片）。判据：

    1. 句子必须以句号/问号/叹号结尾
    2. 句子起点必须是真正的句子边界 —— 紧跟在 `。` 之后，或该片段
       本身以条款标记（第X条 / 第X章 / 括号序号）开头

    这样能过滤掉片段首部被截断的半句。保守策略：宁可少用一条证据，
    也不要输出残句。仍然只是**机械选取**，引文与原文逐字一致。
    """

    if not quote:
        return ""

    # 片段首部是否是合法句子起点（无法确定时视为不合法）
    head_starts_clean = quote.lstrip().startswith(
        ("第", "（", "(", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十")
    )

    best = ""
    cursor = 0
    while cursor < len(quote):
        end = -1
        for index in range(cursor, len(quote)):
            if quote[index] in "。！？":
                end = index + 1
                break
        if end < 0:
            break

        sentence = quote[cursor:end].strip()
        sentence = sentence.lstrip("，、；：）】」』")

        # 起点必须是真边界：cursor==0 且片段开头干净，或紧跟 。
        starts_clean = (
            (cursor == 0 and head_starts_clean)
            or (cursor > 0 and quote[cursor - 1] == "。")
        )

        if (
            starts_clean
            and 15 <= len(sentence) <= limit
            and sentence != "。"
            and len(sentence) > len(best)
        ):
            best = sentence

        cursor = end

    return best


class MockCopywriterLLM:
    """模拟 LLMClient 的 invoke 接口，返回结构化文案 JSON。

    Args:
        max_points: 最多使用几条证据
        quote_chars: 每条要点引用原文的字符数
        inject_forbidden: 测试用 —— 强制注入一段禁止内容
        force_insufficient: 测试用 —— 声明证据不足
        fail_mode: 测试用 —— "invalid_json" / "empty" / "no_evidence_ids"
    """

    def __init__(
        self,
        *,
        max_points: int = 6,
        quote_chars: int = 400,
        inject_forbidden: str | None = None,
        force_insufficient: bool = False,
        fail_mode: str | None = None,
    ):
        self.max_points = max_points
        self.quote_chars = quote_chars
        self.inject_forbidden = inject_forbidden
        self.force_insufficient = force_insufficient
        self.fail_mode = fail_mode
        self.calls: list[dict] = []

    def invoke(self, messages, model=None, **kwargs) -> dict:
        self.calls.append({"messages": messages, "model": model})

        if self.fail_mode == "invalid_json":
            return {"content": "这不是 JSON", "model": "mock-copywriter"}
        if self.fail_mode == "empty":
            return {"content": "", "model": "mock-copywriter"}
        if self.fail_mode == "no_evidence_ids":
            payload = {
                "title": "无引用文案",
                "body": "这段正文没有任何证据引用。",
                "key_points": [
                    {"point_id": "p1", "text": "编造的事实", "evidence_ids": []}
                ],
                "review_notes": [],
            }
            return {
                "content": json.dumps(payload, ensure_ascii=False),
                "model": "mock-copywriter",
            }

        if self.force_insufficient:
            payload = {
                "title": "",
                "body": "",
                "key_points": [],
                "review_notes": [],
                "insufficient_evidence": True,
                "insufficient_reason": (
                    "参考资料只涉及注册规则，缺少针对目标主题的直接条款，"
                    "无法在不编造的前提下成文"
                ),
            }
            return {
                "content": json.dumps(payload, ensure_ascii=False),
                "model": "mock-copywriter",
            }

        payload = self._build(messages)
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "model": "mock-copywriter",
            "usage": {"total_tokens": 0},
        }

    # ---- 内部 ----

    def _parse_prompt(self, messages) -> dict:
        """从 user message 中提取主题、证据块。"""

        user = ""
        for message in messages:
            if message.get("role") == "user":
                user = str(message.get("content") or "")

        info = {"topic": "", "target_chars": 800, "evidence": []}

        for line in user.splitlines():
            stripped = line.strip()
            if stripped.startswith("- 主题："):
                info["topic"] = stripped[len("- 主题：") :].strip()
            elif stripped.startswith("- 目标字数："):
                digits = "".join(
                    ch for ch in stripped if ch.isdigit()
                )
                if digits:
                    info["target_chars"] = int(digits[:4])

        # 解析证据块：每个 [资料 N] 块
        if "===== 参考资料开始" in user:
            block = user.split("===== 参考资料开始", 1)[1]
            block = block.split("===== 参考资料结束", 1)[0]
            current: dict = {}
            for line in block.splitlines():
                stripped = line.strip()
                if stripped.startswith("[资料"):
                    if current:
                        info["evidence"].append(current)
                    current = {}
                    for token in stripped.split():
                        if token.startswith("evidence_id="):
                            current["evidence_id"] = token.split("=", 1)[1]
                elif stripped.startswith("原文："):
                    current["quote"] = stripped[len("原文：") :]
                elif stripped.startswith("关联 claim："):
                    current["claims"] = stripped[len("关联 claim：") :]
            if current:
                info["evidence"].append(current)

        return info

    def _build(self, messages) -> dict:
        info = self._parse_prompt(messages)
        evidence = [
            e for e in info["evidence"] if e.get("evidence_id")
        ][: self.max_points]

        topic = info["topic"] or "政策说明"
        target_chars = info["target_chars"]

        if not evidence:
            return {
                "title": "",
                "body": "",
                "key_points": [],
                "review_notes": [],
                "insufficient_evidence": True,
                "insufficient_reason": "参考资料为空，无法成文",
            }

        key_points = []
        paragraphs = []

        intro = (
            f"参与电力市场交易前，了解相关基本规则有助于减少合规风险。"
            f"本文梳理与「{topic}」直接相关的政策原文表述。"
        )

        # 先把所有可用要点收集起来，再按目标字数裁剪。
        # 裁剪只减少要点数量，不改写引文 —— 保证每条引文都可追溯。
        candidates: list[tuple[str, dict]] = []

        for index, item in enumerate(evidence, start=1):
            quote = _pick_sentence(
                item.get("quote") or "", self.quote_chars
            )
            if not quote:
                # 该证据的原文片段里没有可用的完整句，跳过（不凑数）
                continue

            claim_ids = []
            raw_claims = item.get("claims") or ""
            for token in raw_claims.split(","):
                token = token.strip().rstrip("）").strip()
                if token:
                    claim_ids.append(token)

            text = f"政策原文明确：{quote}"
            candidates.append(
                (
                    text,
                    {
                        "point_id": f"p{len(candidates) + 1}",
                        "text": text,
                        "evidence_ids": [item["evidence_id"]],
                        "claim_ids": claim_ids,
                    },
                )
            )

        closing = (
            "以上表述均来自公开政策原文，具体适用条件、生效时间与适用范围"
            "请以官方发布为准。"
        )
        tail_note = (
            "需要说明的是，本文只覆盖现有政策原文中明确写出的内容。"
            "未涉及的部分不代表政策没有规定，建议查阅完整条款。"
        )

        def assemble(count: int, with_note: bool) -> str:
            picked = [text for text, _ in candidates[:count]]
            parts = [intro, *picked, closing]
            if with_note:
                parts.append(tail_note)
            return "\n\n".join(parts)

        # 选择策略：在不超过 target_chars 的前提下取最多的要点。
        # 若一条都放不下，至少保留 1 条（宁可超出也不出空文案）。
        chosen_count = 0
        chosen_with_note = False

        for with_note in (True, False):
            for count in range(len(candidates), 0, -1):
                body_try = assemble(count, with_note)
                if len(body_try) <= target_chars:
                    chosen_count = count
                    chosen_with_note = with_note
                    break
            if chosen_count:
                break

        if not chosen_count:
            # 放不下完整长度：取 1 条最少内容
            chosen_count = min(1, len(candidates))
            chosen_with_note = False

        for text, point in candidates[:chosen_count]:
            paragraphs.append(text)
            key_points.append(point)

        body = assemble(chosen_count, chosen_with_note)

        if self.inject_forbidden:
            body = body + "\n\n" + self.inject_forbidden

        return {
            "title": topic[:100],
            "body": body,
            "key_points": key_points,
            "review_notes": [
                "本文为程序生成的说明性草稿，需人工核对政策时效与适用范围",
            ],
            "insufficient_evidence": False,
            "insufficient_reason": "",
        }


__all__ = ["MockCopywriterLLM"]
