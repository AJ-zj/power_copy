import json
import logging
import os
import re
import time

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from powercopy.core.budget import TaskBudget
from powercopy.core.retry import RetryPolicy
from powercopy.core.trace import (
    trace_model_attempt,
    record_trace_event,
)

logger = logging.getLogger(__name__)

# 结构化输出格式错误时，追加给模型的修复指令
FORMAT_REPAIR_INSTRUCTION = (
    "上一次返回的内容不是合法 JSON，或者不符合给定的 JSON Schema。"
    "请只返回一个合法的 JSON 对象，不要包含 Markdown 代码块、前后解释或其他文字。"
)


def parse_json_content(content: str):
    """清洗并解析 LLM 返回的 JSON 内容。"""

    # 1. 检查返回内容
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM 返回内容为空或不是字符串")

    # 2. 去除首尾空白
    content = content.strip()

    # 3. 匹配完整的 Markdown JSON 代码块
    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        content,
        flags=re.DOTALL | re.IGNORECASE
    )

    # 4. 如果存在完整围栏，提取内部内容
    if match:
        content = match.group(1).strip()

    # 5. 解析 JSON
    return json.loads(content)


class LLMClient:

    def __init__(self):

        # 加载环境变量
        load_dotenv()

        self.api_key = os.getenv("LLM_API_KEY")
        self.base_url = os.getenv("LLM_BASE_URL")
        self.model = os.getenv("LLM_MODEL")
        self.timeout = float(os.getenv("LLM_TIMEOUT", "60"))

        # 检查必要配置
        if not all([
            self.api_key,
            self.base_url,
            self.model
        ]):
            raise ValueError("LLM 配置不完整")

        # 初始化模型客户端
        # max_retries=0：重试由我们自己的 RetryPolicy 管理，
        # 避免 SDK 内部再次重试导致实际调用次数失控。
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0
        )

        # 重试策略
        self.retry_policy = RetryPolicy(
            max_attempts=3,
            base_delay=1.0,
            max_delay=10.0
        )

    @trace_model_attempt
    def _invoke_once(
        self,
        messages,
        model=None,
        *,
        tools=None
    ):
        """单次模型请求（不带重试）

        model 参数只是入口名；平台实际路由到哪个模型，
        以返回值里的 "model" 字段为准（见 V1 实测记录）。
        """

        # 1. 构建 API 请求参数
        request = {
            "model": model or self.model,
            "messages": messages
        }

        if tools is not None:
            request["tools"] = tools

        # 2. 调用模型
        response = self.client.chat.completions.create(
            **request
        )

        choice = response.choices[0]
        message = choice.message
        usage = response.usage

        # 3. 保留原有返回结构
        result = {
            "content": message.content,
            "model": response.model,
            "input_tokens": (
                usage.prompt_tokens if usage else None
            ),
            "output_tokens": (
                usage.completion_tokens if usage else None
            ),
            "total_tokens": (
                usage.total_tokens if usage else None
            )
        }

        # 4. 提取工具调用请求
        if tools is not None:

            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments
                    }
                }
                for call in (message.tool_calls or [])
            ]

            result["finish_reason"] = choice.finish_reason

        return result

    def _execute(
        self,
        messages,
        model=None,
        schema=None,
        *,
        tools=None,
        budget: TaskBudget | None = None
    ):
        """统一管理模型请求、输出校验与重试。"""

        policy = self.retry_policy
        selected_model = model or self.model

        total_start = time.perf_counter()
        known_total_tokens = 0
        usage_incomplete = False
        format_retries = 0
        max_format_retries = 1

        # 浅拷贝：后续追加修复指令不会修改调用者传入的列表
        request_messages = list(messages)

        for attempt in range(1, policy.max_attempts + 1):

            # 本次新增：预算检查放在 try 外部，
            # 避免 BudgetExceeded 被当成普通 API 异常处理
            if budget is not None:
                budget.reserve_api_attempt()

            start = time.perf_counter()
            stage = "api"

            try:
                # 1. 单次 API 请求
                request_options = {}
                if tools is not None:
                    request_options["tools"] = tools

                result = self._invoke_once(
                    request_messages,
                    model=selected_model,
                    **request_options
                )

                # 2. 累计已知 Token 消耗
                tokens = result.get("total_tokens")
                if tokens is not None:
                    known_total_tokens += tokens
                else:
                    usage_incomplete = True

                if budget is not None:
                    budget.record_usage(tokens)

                # 3. 按需执行结构化校验
                stage = "output"
                if schema is not None:
                    data = parse_json_content(
                        result["content"]
                    )
                    result["data"] = schema.model_validate(data)

                # 4. 返回结果与统计信息
                latency_ms = round(
                    (time.perf_counter() - start) * 1000,
                    2
                )
                total_latency_ms = round(
                    (time.perf_counter() - total_start) * 1000,
                    2
                )

                logger.info(
                    "LLM success model=%s attempt=%s "
                    "latency_ms=%s total_tokens=%s",
                    selected_model,
                    attempt,
                    latency_ms,
                    known_total_tokens
                )

                return {
                    **result,
                    "attempts": attempt,
                    "retry_count": attempt - 1,
                    "format_retries": format_retries,
                    "latency_ms": latency_ms,
                    "total_latency_ms": total_latency_ms,
                    "total_tokens": (
                        None if usage_incomplete else known_total_tokens
                    ),
                    "known_total_tokens": known_total_tokens,
                    "usage_incomplete": usage_incomplete,
                }

            except Exception as error:

                latency_ms = round(
                    (time.perf_counter() - start) * 1000,
                    2
                )

                logger.warning(
                    "LLM failed model=%s attempt=%s stage=%s "
                    "error_type=%s latency_ms=%s",
                    selected_model,
                    attempt,
                    stage,
                    type(error).__name__,
                    latency_ms
                )

                # API 阶段失败：这次请求的用量未知。
                # 只标记任务级预算，不改变本次返回的 total_tokens 语义：
                # 返回值反映的是「成功那次响应」的用量，预算才反映全局缺口。
                if stage == "api":
                    if budget is not None:
                        budget.record_usage(None)

                # ---- 结构化输出格式错误：追加修复指令后重试 ----
                if (
                    stage == "output"
                    and schema is not None
                    and isinstance(
                        error,
                        (json.JSONDecodeError, ValidationError, ValueError)
                    )
                ):
                    format_retries += 1

                    if format_retries > max_format_retries:
                        raise

                    # 全局预算耗尽就不必准备下一轮
                    if budget is not None:
                        budget.check()

                    logger.info(
                        "LLM format retry %s/%s",
                        format_retries,
                        max_format_retries
                    )

                    request_messages.append({
                        "role": "user",
                        "content": FORMAT_REPAIR_INSTRUCTION
                    })
                    continue

                # ---- API 异常：交给 RetryPolicy 判断 ----
                if not policy.should_retry(error):
                    raise

                # 达到最大尝试次数，停止重试
                if attempt >= policy.max_attempts:
                    raise

                # 全局预算耗尽就不必等待下一轮
                if budget is not None:
                    budget.check()

                # 计算等待时间
                delay = policy.get_delay(attempt)

                logger.info(
                    "LLM retry waiting %.2f seconds",
                    delay
                )

                time.sleep(delay)

        # 理论上不会到这里：循环内要么返回要么抛出
        raise RuntimeError("LLM 执行流程异常结束")

    def invoke(
        self,
        messages,
        model=None,
        *,
        tools=None,
        budget: TaskBudget | None = None
    ):
        """普通文本调用入口（重试由 _execute 统一管理）。"""
        return self._execute(
            messages=messages,
            model=model,
            tools=tools,
            budget=budget
        )

    def invoke_structured(
        self,
        messages,
        schema: type[BaseModel],
        model=None,
        *,
        budget: TaskBudget | None = None
    ):
        """结构化输出入口（复用 _execute，避免嵌套重试）。"""

        json_schema = schema.model_json_schema()

        instruction = (
            "请严格按照以下 JSON Schema 返回结果。"
            "只返回 JSON，不要返回 Markdown 或额外解释。\n"
            + json.dumps(
                json_schema,
                ensure_ascii=False
            )
        )

        request_messages = [
            *messages,
            {
                "role": "user",
                "content": instruction
            }
        ]

        return self._execute(
            messages=request_messages,
            model=model,
            schema=schema,
            budget=budget
        )


if __name__ == "__main__":

    llm = LLMClient()

    messages = [
        {
            "role": "system",
            "content": "你是PowerCopy售电文案助手。"
        },
        {
            "role": "user",
            "content": "请用一句话介绍你的职责。"
        }
    ]

    result = llm.invoke(messages)

    print(result)
