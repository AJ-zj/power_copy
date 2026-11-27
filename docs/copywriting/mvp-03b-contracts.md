# MVP-03-B 数据契约

**版本**：`content-draft-v1`
**日期**：2026-11-27
**用途**：供 Sol 的 Content Studio 前端消费

---

## 一、输入契约 `CopywritingRequest`

```python
class CopywritingRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    topic: str                      # 1-200 字符，必填
    target_audience: str = ""       # ≤100 字符
    platform: Platform = GENERAL    # wechat_mp / website / linkedin / general
    target_chars: int = 800         # 100-5000
    style: str = "专业、通俗、可信"   # ≤200 字符
    purpose: str = ""               # ≤500 字符
    evidence_package: EvidencePackage   # 必填，内嵌不接受外部引用
    budget: CopywritingBudget
```

### 安全约束

| 约束 | 实现 |
|---|---|
| 不接受文件路径 | 无 path 字段；`extra="forbid"` |
| 不接受 URL | 同上 |
| 不接受密钥 | 同上 |
| 不做类型强转 | `strict=True` |

实测：`db_path` / `index_path` / `url` / `llm_api_key` / `file` 均被拒绝。

### 预算契约

```python
max_api_attempts: int = 4      # 1-20
max_known_tokens: int = 12_000 # 500-100000
```

---

## 二、输出契约 `ContentDraft`

```python
class ContentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str            # draft-<16 hex>
    title: str               # ≤100 字符
    body: str                # ≤20000 字符
    key_points: list[KeyPoint]

    # 请求回显
    topic: str
    target_audience: str
    platform: str
    style: str
    target_chars: int

    review_notes: list[str]  # 必须人工确认的事项
    status: CopywritingStatus
    status_reason: str

    evidence_refs: list[str] # 去重的 evidence_id 集合
    research_id: str         # 来源证据包的研究 ID

    # 政策时效与适用性（原样保留，不得弱化）
    policy_validity_status: str
    policy_validity_note: str
    region_applicability: str

    # 生成溯源
    generation_mode: GenerationMode   # mock / live
    model_name: str
    prompt_version: str               # copywriting-v1
    token_usage: dict

    created_at: str          # ISO 8601 UTC
    schema_version: str      # content-draft-v1
```

### `KeyPoint`

```python
class KeyPoint(BaseModel):
    point_id: str
    text: str                     # ≤600 字符
    evidence_ids: list[str]       # **至少 1 个**，最多 6 个，不得重复
    claim_ids: list[str] = []     # 对应 claim，可追溯
    semantic_status: str = "pending_review"
```

**关键**：`evidence_ids` 为空会被契约层直接拒绝。这保证「每条事实都有引用」。

### `CopywritingStatus`

| 值 | 含义 |
|---|---|
| `pending_review` | **默认且正常状态**。已生成，等待人工审核 |
| `failed` | 生成失败（模型输出无效、引用校验失败、命中禁用内容） |
| `aborted` | 主动停止（证据不足以支撑目标范围） |

**前端注意**：`pending_review` 不是错误状态，不要渲染成失败。

---

## 三、三层统计（不可合并）

```python
draft.statistics()
```

| 字段 | 含义 |
|---|---|
| `key_points_total` | 要点总数 |
| `key_points_bound_to_evidence` | 已绑定真实证据 ID 的数量 |
| `key_points_pending_semantic_review` | 语义支持性待人工确认 |
| `key_points_human_confirmed` | **恒为 0**（人工确认通道未实现） |
| `human_confirmation_available` | **恒为 False** |
| `char_count` | 正文字符数（不含空白） |

**为什么分开**：`bound_to_evidence` 只表示引文真实存在，不表示语义成立。
把两者合并成一个「可信事实数」会高估可信度。

---

## 四、防护机制

### 引用校验（硬失败）

`validate_citations` 检查：

1. `evidence_id` 必须存在于证据包
2. 该证据必须被**未被拒绝**的 claim 引用
3. 未通过 → `CitationViolation` → 草稿 `failed`

**被拒绝 claim 的路径不可用**：即使该 evidence 存在，只要引用它的 claim 全被拒，
就不能用于文案。

### 禁用内容检测

`check_forbidden_content` 检测并回溯证据原文：

| code | 类别 | 严重度 |
|---|---|---|
| `SAVINGS_PROMISE` | 节省比例承诺 | error |
| `REVENUE_PROMISE` | 收益承诺 | error |
| `PRICE_QUOTE` | 具体报价 | error |
| `SERVICE_PROMISE` | 企业服务承诺 | error |
| `CLIENT_CASE` | 客户案例 | error |
| `SUCCESS_CASE` | 成功案例暗示 | error |
| `POLICY_CLAUSE` | 政策条款引用 | warn |
| `POLICY_VALIDITY` | 政策有效期 | warn |
| `POLICY_REGION` | 政策适用地区 | warn |
| `ABSOLUTE_CLAIM` | 绝对化表述 | warn |

**局限（必须认知）**：

- 字面检查，无法识别语义等价改写
- 无法发现「引用了真实条款但断章取义」
- 无法发现暗示性承诺
- `GuardReport.ok == True` **不等于**文案合规

---

## 五、JSON 示例

```json
{
  "draft_id": "draft-53a7a409878b4e71",
  "title": "工商业企业参与电力市场前，需要了解哪些基本规则？",
  "body": "参与电力市场交易前，了解相关基本规则有助于减少合规风险。...",
  "key_points": [
    {
      "point_id": "p1",
      "text": "政策原文明确：第二十条 各地按照国家有关规定确定参与辅助服务市场的准入条件时，应当实行公平准入...",
      "evidence_ids": ["0ba839925733e6fd"],
      "claim_ids": ["c-q4-4"],
      "semantic_status": "pending_review"
    }
  ],
  "topic": "工商业企业参与电力市场前，需要了解哪些基本规则？",
  "target_audience": "工商业用电企业",
  "platform": "wechat_mp",
  "style": "专业、通俗、可信",
  "target_chars": 800,
  "review_notes": [
    "本文为程序生成的说明性草稿，需人工核对政策时效与适用范围",
    "政策时效：引用的是本地库中该政策的最新观测版本；这不代表该政策现行有效，是否废止需以官方公告为准"
  ],
  "status": "pending_review",
  "status_reason": "生成成功。6 条事实要点已绑定真实证据；语义支持性待人工确认",
  "evidence_refs": ["0ba839925733e6fd", "27c7cc01407c39c4"],
  "research_id": "res-6a7e1a17c5154190",
  "policy_validity_status": "latest_observed",
  "policy_validity_note": "引用的是本地库中该政策的最新观测版本；这不代表该政策现行有效，是否废止需以官方公告为准",
  "region_applicability": "",
  "generation_mode": "mock",
  "model_name": "mock",
  "prompt_version": "copywriting-v1",
  "token_usage": {
    "input_tokens": null,
    "output_tokens": null,
    "total_tokens": 0,
    "api_attempts": 1,
    "known_total_tokens": 0,
    "usage_incomplete": false
  },
  "created_at": "2026-11-27T01:12:34+00:00",
  "schema_version": "content-draft-v1"
}
```

---

## 六、前端集成建议

### Content Studio 需要展示的要素

1. **标题 + 正文**（可编辑）
2. **事实要点 ↔ 证据映射**：点击要点高亮对应的原文片段
3. **待审核事项**（`review_notes`）置顶，不可折叠
4. **政策时效与适用性**警示条（不可隐藏）
5. **状态徽章**：`pending_review` / `failed` / `aborted`
6. **生成溯源**：mode / model / prompt_version / token_usage

### 必须避免的 UI 陷阱

| 陷阱 | 后果 |
|---|---|
| 把 `pending_review` 显示为「成功」 | 用户以为可发布 |
| 隐藏 `policy_validity_note` | 用户忽略时效风险 |
| 不展示 `key_points` 的证据绑定 | 无法核对 |
| 把 `human_confirmed=0` 显示成「0 条已验证」 | 误导为「没有事实」 |

### 数据获取

`EvidencePackage` 与 `ContentDraft` 都可以 `model_dump(mode="json")` 直接序列化，
支持 JSON 往返（输出契约未使用 `strict=True`，正是为此）。

---

## 七、与 MVP-02 的关系

```text
ResearchRequest ──► ResearchAgent ──► EvidencePackage
                                            │
                                            ▼
CopywritingRequest (内嵌 EvidencePackage) ──► CopywritingAgent ──► ContentDraft
```

`ContentDraft.claims` 通过 `key_points[].claim_ids` 与
`EvidencePackage.claims[].claim_id` 对应，实现端到端追溯：

```text
文案句子 → key_point → claim_id → claim → evidence_id → chunk_id
        → policy_id + version + start/end → 政策原文
```
