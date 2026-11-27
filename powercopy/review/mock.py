"""离线 Mock 审核器与修订器。

设计
----
**Mock 审核器不做语义判断**，只返回「我无法判断」：

```python
{"verdicts": []}   # 空 → verifier 视为未判断，事实支持性保持 pending_review
```

这样 Mock 模式下的行为是诚实的：程序做字面回溯，语义支持性标记待人工，
而不是假装 Mock 能判断语义。

**Mock 修订器**做确定性的文本订正：

1. 删除被标记为无依据的句子
2. 补上时效免责声明（若缺失）
3. 返回 `addressed` 列表

它不"创作"，只做删减与追加，因此不会引入新事实。
"""

from __future__ import annotations

import json
import re


class MockReviewerLLM:
    """模拟事实支持性判断的 LLM。

    默认返回空 verdicts（表示「机器无法判断」），
    使 `ContentVerifier` 保持 `pending_review`。

    Args:
        verdicts: 测试用 —— 强制给出判断列表
        fail_mode: 测试用 —— "invalid_json" / "empty"
    """

    def __init__(
        self,
        *,
        verdicts: list[dict] | None = None,
        fail_mode: str | None = None,
    ):
        self.verdicts = verdicts
        self.fail_mode = fail_mode
        self.calls: list[dict] = []

    def invoke(self, messages, model=None, **kwargs) -> dict:
        budget = kwargs.get("budget")
        if budget is not None:
            budget.reserve_api_attempt()

        self.calls.append({"messages": messages, "model": model})

        if budget is not None:
            budget.record_usage(0)

        if self.fail_mode == "invalid_json":
            return {"content": "不是 JSON", "model": "mock-reviewer"}
        if self.fail_mode == "empty":
            return {"content": "", "model": "mock-reviewer"}

        payload = {"verdicts": self.verdicts or []}
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "model": "mock-reviewer",
        }


# 需要删除的「无依据表述」模式（与 guards 的 error 级对应）
_REMOVABLE_PATTERNS = [
    re.compile(r"[^。！？\n]*(?:节省|降低|减少|节约)[^。，；\n]{0,8}\d+(?:\.\d+)?\s*(?:%|％|个百分点|成)[^。！？\n]*[。！？]?"),
    re.compile(r"[^。！？\n]*(?:增收|盈利|回报|收益)[^。，；\n]{0,8}\d+(?:\.\d+)?\s*(?:%|％|万元|元)[^。！？\n]*[。！？]?"),
    re.compile(r"[^。！？\n]*\d+(?:\.\d+)?\s*元\s*/\s*(?:千瓦时|度|kWh|MWh)[^。！？\n]*[。！？]?"),
    re.compile(r"[^。！？\n]*(?:我方|本公司|我们|我司)\s*(?:可以|能够|将|承诺|保证|提供)[^。，；\n]{0,20}(?:服务|方案|代办|代理|咨询|优惠)[^。！？\n]*[。！？]?"),
    re.compile(r"[^。！？\n]*(?:成功案例|服务过|已帮助|众多客户|客户见证)[^。！？\n]*[。！？]?"),
    re.compile(r"[^。！？\n]*(?:现行有效|仍然有效|持续有效|长期有效)[^。！？\n]*[。！？]?"),
]

_DISCLAIMER = (
    "以上表述均来自公开政策原文，具体适用条件、生效时间与适用范围"
    "请以官方发布为准。"
)


class MockReviserLLM:
    """确定性的文案订正器。

    Args:
        drop_unsupported: 是否删除无依据表述（默认 True）
        add_disclaimer: 是否补时效免责声明（默认 True）
        fail_mode: 测试用 —— "invalid_json" / "empty" / "no_change"
    """

    def __init__(
        self,
        *,
        drop_unsupported: bool = True,
        add_disclaimer: bool = True,
        fail_mode: str | None = None,
    ):
        self.drop_unsupported = drop_unsupported
        self.add_disclaimer = add_disclaimer
        self.fail_mode = fail_mode
        self.calls: list[dict] = []

    def invoke(self, messages, model=None, **kwargs) -> dict:
        budget = kwargs.get("budget")
        if budget is not None:
            budget.reserve_api_attempt()

        self.calls.append({"messages": messages, "model": model})

        if budget is not None:
            budget.record_usage(0)

        if self.fail_mode == "invalid_json":
            return {"content": "不是 JSON", "model": "mock-reviser"}
        if self.fail_mode == "empty":
            return {"content": "", "model": "mock-reviser"}

        user = ""
        for message in messages:
            if message.get("role") == "user":
                user = str(message.get("content") or "")

        title, body = self._parse(user)
        addressed: list[str] = []
        note_parts: list[str] = []

        # 提取 issue_id 列表
        issue_ids = re.findall(r"\[(i\d+)\]", user)

        if self.drop_unsupported:
            removed = 0
            for pattern in _REMOVABLE_PATTERNS:
                new_body, count = pattern.subn("", body)
                if count:
                    removed += count
                    body = new_body
            if removed:
                addressed.extend(issue_ids[:removed])
                note_parts.append(f"删除 {removed} 处无依据表述")
            body = re.sub(r"\n{3,}", "\n\n", body).strip()

        if self.add_disclaimer and "官方" not in body and "不代表" not in body:
            body = body + "\n\n" + _DISCLAIMER
            if issue_ids:
                addressed.append(issue_ids[-1])
            note_parts.append("补充时效免责声明")

        if self.fail_mode == "no_change":
            note_parts.append("未做修改")

        payload = {
            "title": title,
            "body": body,
            "addressed": sorted(set(addressed)),
            "note": "；".join(note_parts) or "未做修改",
        }
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "model": "mock-reviser",
        }

    @staticmethod
    def _parse(user: str) -> tuple[str, str]:
        """从 prompt 中解析标题与正文。"""

        title = ""
        body_lines: list[str] = []
        mode = None

        for line in user.splitlines():
            stripped = line.strip()
            if stripped.startswith("标题："):
                title = stripped[len("标题：") :].strip()
                continue
            if stripped == "正文：":
                mode = "body"
                continue
            if stripped.startswith("【审核发现的问题】"):
                break
            if mode == "body":
                body_lines.append(line)

        return title, "\n".join(body_lines).strip()


__all__ = ["MockReviewerLLM", "MockReviserLLM"]
