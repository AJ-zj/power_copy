# ADR-001：LLM 接口协议保持 Chat Completions

- **状态**：已采纳
- **日期**：2026-11-26
- **决策者**：项目负责人（待确认）
- **相关阶段**：阶段01 LLM Client

---

## 背景

OpenAI 于 2025 年推出 Responses API，官方将其定位为新项目的推荐接口，Chat Completions 进入长期维护而非废弃状态。项目当前使用 `chat.completions.create`。

需要决定：是否迁移到 Responses API。

---

## 备选方案

### 方案 A：保持 Chat Completions（当前）

**优点**
- 已在 26 个模型的平台上验证可用
- 消息结构简单，`messages` 数组自管理
- 第三方兼容平台普遍支持

**缺点**
- 不支持 `previous_response_id` 服务端会话
- 不提供 `output_text` 便捷属性
- 官方定位为"维护模式"

### 方案 B：迁移到 Responses API

**优点**
- 官方推荐
- 支持服务端会话持久化（Conversations API）
- 更清晰的多模态输出结构

**缺点**
- **第三方平台支持未验证**
- 需重写消息管理与结果解析
- 迁移涉及三项改动：端点、output 数组解析、状态传递方式

### 方案 C：双协议抽象层

**优点**
- 两种协议都支持
- 未来切换成本低

**缺点**
- 抽象层本身是复杂度
- 当前只有一种协议实际可用，抽象无意义

---

## 决策

**采纳方案 A：保持 Chat Completions。**

理由：

1. **平台能力未验证**。`/v1/models` 返回 `supported_endpoint_types: ["openai"]`，但这只说明兼容 OpenAI 协议，不保证支持 `/v1/responses` 端点。在未验证前迁移，风险不可控。

2. **收益与当前需求不匹配**。Responses API 的核心优势是服务端会话持久化。但 PowerCopy 的 Agent Loop 需要精确控制消息历史（tool_calls 与 tool 消息必须严格配对），服务端托管反而削弱可控性。

3. **迁移成本明确但不产生业务价值**。当前 LLM Client 仅 400 行，工作正常，820 项测试通过。迁移收益仅为"符合官方推荐"。

4. **官方未宣布废弃**。Chat Completions 仍是长期支持接口，不存在必须迁移的时间压力。

---

## 后果

### 正面

- 零迁移成本
- 现有测试与工作台不受影响
- 第三方兼容平台（易拓AI）的可用性已验证

### 负面

- 无法使用服务端会话管理
- 若未来 OpenAI 收紧支持，需重新评估

### 缓解

在 `LLMClient` 保持当前结构的前提下，为未来迁移预留空间：

- `_invoke_once` 是唯一的 API 调用点，迁移只需改这一处
- 返回值已标准化为 `{content, model, input_tokens, output_tokens, total_tokens, tool_calls, finish_reason}`，与协议解耦

---

## 重新评估触发条件

满足任一条件时重新评估：

1. **平台验证支持 Responses** —— `GET /v1/responses` 返回非 404
2. **项目迁移到 OpenAI 直连** —— 不再依赖第三方兼容平台
3. **需要多模态输入** —— Responses API 的图像/文件处理更成熟
4. **需要服务端会话持久化** —— 例如长对话场景
5. **官方宣布 Chat Completions 进入废弃周期**

---

## 参考

- [OpenAI Responses API 迁移指南](https://developers.openai.com/api/docs/guides/migrate-to-responses)（访问 2026-11-26）
- [Azure OpenAI 迁移文档](https://learn.microsoft.com/azure/developer/ai/how-to/azure-openai-to-responses)（访问 2026-11-26）
