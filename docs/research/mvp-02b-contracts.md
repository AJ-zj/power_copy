# MVP-02-B 数据契约

**版本**：`evidence-package-v1`
**日期**：2026-11-27

---

## 一、输入契约 `ResearchRequest`

```python
class ResearchRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)

    topic: str                    # 1-200 字符，必填
    target_audience: str = ""     # ≤100 字符
    region: str = ""              # ≤40 字符
    research_objective: str = ""  # ≤1000 字符
    required_questions: list[str] = []   # ≤12 条，每条第 1-300 字符，不得重复
    task_budget: ResearchBudget
```

### 安全约束

| 约束 | 实现 |
|---|---|
| 不接受文件路径 | 无 path 字段；`extra="forbid"` |
| 不接受数据库路径 | 同上 |
| 不接受外部网站 URL | 同上 |
| 不接受模型/索引路径 | 同上 |
| 不接受密钥 | 同上 |
| 不做类型强转 | `strict=True`（`max_rounds="6"` 会被拒绝） |

实测：传入 `db_path` / `index_path` / `url` / `file` / `llm_api_key` /
`model_path` 任一字段均抛 `ValidationError`。

### 预算契约 `ResearchBudget`

```python
max_rounds: int = 6              # 1-12
max_tool_calls: int = 12         # 1-30
max_api_attempts: int = 10       # 1-30
max_known_tokens: int = 30_000   # 1000-200000
max_questions: int = 8           # 1-12
max_evidence_per_question: int = 5   # 1-10
```

预算由程序强制，模型无法突破。

---

## 二、输出契约 `EvidencePackage`

```python
class EvidencePackage(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 不 strict，支持 JSON 往返

    research_id: str                       # res-<16 hex>
    topic: str
    region: str = ""
    target_audience: str = ""
    research_questions: list[ResearchQuestion]
    claims: list[Claim]
    evidence_items: list[EvidenceItem]
    unanswered_questions: list[str]
    uncertainties: list[str]
    policy_validity_status: PolicyValidityStatus
    policy_validity_note: str
    research_status: ResearchStatus
    status_reason: str
    verification_issues: list[VerificationIssue]
    trace_reference: dict
    created_at: str                        # ISO 8601 UTC
    schema_version: str = "evidence-package-v1"
```

### `ResearchQuestion`

```python
question_id: str      # q1, q2...
text: str
answered: bool
answer: str = ""
claim_ids: list[str] = []
```

### `Claim`（候选事实）

```python
claim_id: str
question_id: str
statement: str            # 1-1000 字符
evidence_ids: list[str]   # **至少 1 个**，最多 8 个，不得重复
status: ClaimStatus
support_note: str = ""
```

### `EvidenceItem`（证据）

```python
evidence_id: str          # 程序生成，16 hex
policy_id: str
document_id: str
version: int              # ≥1
chunk_id: str
source_url: str
title: str
exact_quote: str          # 必须与 body[start:end] 完全一致
start: int
end: int                  # end - start == len(exact_quote)
page: int | None
content_sha256: str       # 64 hex
status: EvidenceStatus
```

---

## 三、关键枚举

### `ClaimStatus`

| 值 | 含义 |
|---|---|
| `candidate` | 模型提出，未核验（中间态，不进入最终包） |
| `verified` | 保留值；当前流程不使用（见下） |
| `pending_review` | **引文已核验与原文一致，语义支持性需人工确认** |
| `rejected` | 引文核验失败或引用了无效证据 |

**为什么最终包里的正常值是 `pending_review` 而不是 `verified`**：

程序只能验证「这段文字确实在原文的这个位置」，无法验证
「这段文字是否真的支持该结论」。因此把语义支持性交给人工，
避免用程序核验通过来暗示语义成立。

### `EvidenceStatus`

| 值 | 含义 |
|---|---|
| `candidate` | 仅来自检索候选，未做原文字符核验 |
| `verified` | 原文已重新读取，偏移与引文均已核验 |

### `PolicyValidityStatus`

| 值 | 含义 |
|---|---|
| `latest_observed` | 本地库中该政策的最新观测版本。**不表示现行有效** |
| `superseded_in_corpus` | 库中存在更新的版本 |
| `unknown` | 无法确认 |

**没有 `currently_effective` 这个值**，这是刻意的：本地库只有观测记录，
没有废止信息，任何「现行有效」的判断都缺乏依据。

### `ResearchStatus`

| 值 | 含义 |
|---|---|
| `completed` | 全部问题解决，无拒绝、无待确认 |
| `partial` | 部分完成：有未解决问题 / 待确认 / 被拒绝事实 |
| `failed` | 研究失败：拆解失败、检索不可用等 |

---

## 四、`evidence_id` 生成规则

```python
evidence_id = sha256(
    f"{policy_id}|{version}|{chunk_id}|{start}|{end}"
).hexdigest()[:16]
```

性质：

1. **确定性**：同样的 canonical 字段永远得到同样的 ID
2. **不可伪造**：模型编造一个 16 hex 字符串，除非恰好等于真实证据的
   canonical 组合哈希，否则核验阶段会被 `EVIDENCE_ID_MISMATCH` 拒绝
3. **不含随机性**：便于跨运行比对

---

## 五、核验问题代码

| 代码 | 含义 |
|---|---|
| `CHUNK_NOT_IN_MANIFEST` | chunk_id 不在当前切片清单中（索引可能过期） |
| `EVIDENCE_ID_MISMATCH` | evidence_id 与 canonical 值不符，或元信息与清单不符 |
| `POLICY_NOT_FOUND` | policy_id 在库中不存在 |
| `VERSION_NOT_FOUND` | 该 policy 没有该 version |
| `DOCUMENT_ID_MISMATCH` | document_id 与库中记录不符 |
| `OFFSET_OUT_OF_RANGE` | [start, end) 超出正文长度 |
| `QUOTE_MISMATCH` | exact_quote 与 body[start:end] 不一致 |
| `CONTENT_SHA_MISMATCH` | content_sha256 与库中记录不一致 |
| `CLAIM_WITHOUT_EVIDENCE` | claim 未关联证据（契约层已阻止，防御性保留） |
| `CLAIM_REFERENCES_BAD_EVIDENCE` | claim 引用了未通过核验的证据 |
| `TRUNCATED_CANDIDATE` | 候选正文被截断，不能作为完整引文 |
| `CANDIDATE_INVALID` | 候选无法构造为证据 |

---

## 六、JSON 示例（节选）

```json
{
  "research_id": "res-9437375ad39c4271",
  "topic": "工商业企业参与电力市场前，需要了解哪些基本规则？",
  "research_status": "partial",
  "status_reason": "全部问题均有证据支撑，但 15 条事实的语义支持性待人工确认",
  "policy_validity_status": "latest_observed",
  "policy_validity_note": "引用的是本地库中该政策的最新观测版本；这不代表该政策现行有效，是否废止需以官方公告为准",
  "research_questions": [
    {"question_id": "q1", "text": "...", "answered": true, "claim_ids": ["c-q1-1"]}
  ],
  "claims": [
    {
      "claim_id": "c-q1-1",
      "question_id": "q1",
      "statement": "政策原文表述为：经营主体注册管理水平，我们组织制定了...",
      "evidence_ids": ["bfa4cb74c5bd64c3"],
      "status": "pending_review",
      "support_note": "引文已核验与原文一致；语义支持性需人工确认"
    }
  ],
  "evidence_items": [
    {
      "evidence_id": "bfa4cb74c5bd64c3",
      "policy_id": "00aaf3720779431692a2d422fc7eec49",
      "document_id": "a459ff64-c41c-4b51-9cfe-a84e3892e90e",
      "version": 1,
      "chunk_id": "514cd911afaf6ba3...",
      "source_url": "https://zfxxgk.nea.gov.cn/2024-09/13/c_1212399683.htm",
      "title": "国家能源局关于印发《电力市场注册基本规则》的通知",
      "exact_quote": "经营主体注册管理水平，我们组织制定了《电力市场注册基本规则》...",
      "start": 465,
      "end": 897,
      "page": null,
      "content_sha256": "6de6df7674d427c5e0a21ffb4202a6e15607bf574caade5e4c6716026ce5e9f8",
      "status": "verified"
    }
  ],
  "unanswered_questions": [],
  "uncertainties": [],
  "verification_issues": [],
  "trace_reference": {},
  "created_at": "2026-11-27T00:12:34+00:00",
  "schema_version": "evidence-package-v1"
}
```
