# Content Studio Workbench API

## Status

`GET /api/workbench/content/status` returns `{"data": ...}`.

- `workflow_available` reports whether the committed `powercopy.workflows.content` service can be imported.
- `data_available` reports whether the configured policy database and manifest, lexical index, and dense index are present.
- `mock_available` is true only when both the workflow and required local data are present.
- `live_available` also requires the server-side `POWERCOPY_WORKBENCH_ALLOW_LIVE=1` gate and complete server model configuration.
- `review_available`, `maximum_automatic_revisions`, `human_confirmation_available`, and `release_available` expose actual workflow capabilities. No automated publishing endpoint exists.

## Run

`POST /api/workbench/content/runs` accepts a JSON object with unknown fields rejected:

```json
{
  "topic": "工商业企业参与电力市场前需要了解哪些基本规则",
  "target_audience": "工商业用电企业",
  "region": "安徽",
  "platform": "wechat_mp",
  "target_chars": 800,
  "style": "专业、通俗、可信",
  "purpose": "政策科普",
  "mode": "mock",
  "confirm_live": false,
  "enable_review": true,
  "enable_revision": true,
  "max_revisions": 1,
  "max_api_attempts": 12,
  "max_known_tokens": 40000,
  "max_questions": 5,
  "max_tool_calls": 8
}
```

Successful requests return the serialized `ContentWorkflowResult` plus aliases used by the page:

- `evidence_package` and `research_statistics` contain Research output and distinguish `claims_text_verified` from `claims_pending_semantic_review`. The latter is not human confirmation.
- `content_draft` and `draft_statistics` contain the editable draft, citations, review notes, policy validity limitations, and provenance.
- `review_report`, `review_statistics`, `revision_records`, `budget`, `stage_timings_ms`, `requires_human`, and `schema_version` map to the service result.

`pending_review` is a normal non-publishable result. A Live request additionally needs request-level `confirm_live: true` and the server gate. API attempts are a hard limit within the budget object used by each model request. In the integrated workflow, draft generation still creates a separate budget, so the shared snapshot is incomplete across stages. Known tokens are a soft budget when the provider returns usage.

Errors use the normal workbench envelope `{"error":{"code":"...","message":"..."}}`. The page handles `CONTENT_WORKFLOW_UNAVAILABLE`, `CONTENT_DATA_UNAVAILABLE`, `LIVE_MODEL_DISABLED`, `CONTENT_RUN_FAILED`, request validation and network failure.

The UI does not provide fake output when this capability is unavailable. Contract fixtures belong only in isolated API tests and must not be treated as integration results.
