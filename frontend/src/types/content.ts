export type ContentPlatform = 'wechat_mp' | 'website' | 'linkedin' | 'general'
export type ContentMode = 'mock' | 'live'
export type DraftStatus = 'pending_review' | 'failed' | 'aborted'
export type PolicyValidity = 'latest_observed' | 'superseded_in_corpus' | 'unknown'
export type ClaimStatus = 'candidate' | 'verified' | 'pending_review' | 'rejected'

export interface ContentStatus {
  mock_available: boolean
  live_available: boolean
  workflow_available: boolean
}

export interface ContentRequest {
  topic: string
  target_audience: string
  region: string
  platform: ContentPlatform
  target_chars: number
  style: string
  purpose: string
  mode: ContentMode
  confirm_live: boolean
}

export interface ResearchQuestion {
  question_id: string
  text: string
  answered: boolean
  answer: string
  claim_ids: string[]
}

export interface ResearchClaim {
  claim_id: string
  question_id: string
  statement: string
  evidence_ids: string[]
  status: ClaimStatus
  support_note: string
}

export interface EvidenceItem {
  evidence_id: string
  policy_id: string
  document_id: string
  version: number
  chunk_id: string
  source_url: string
  title: string
  exact_quote: string
  start: number
  end: number
  page: number | null
  content_sha256: string
  status: 'candidate' | 'verified'
}

export interface EvidencePackage {
  research_id: string
  topic: string
  region: string
  target_audience: string
  research_questions: ResearchQuestion[]
  claims: ResearchClaim[]
  evidence_items: EvidenceItem[]
  unanswered_questions: string[]
  uncertainties: string[]
  policy_validity_status: PolicyValidity
  policy_validity_note: string
  research_status: string
  status_reason: string
  verification_issues: Array<{ code: string; detail?: string }>
  trace_reference: Record<string, unknown>
  created_at: string
  schema_version: string
}

export interface ResearchStatistics {
  claims_total: number
  claims_text_verified: number
  claims_pending_semantic_review: number
  claims_human_confirmed: number
  claims_rejected: number
  claims_candidate: number
  human_confirmation_available: boolean
}

export interface KeyPoint {
  point_id: string
  text: string
  evidence_ids: string[]
  claim_ids: string[]
  semantic_status: string
}

export interface ContentDraft {
  draft_id: string
  title: string
  body: string
  key_points: KeyPoint[]
  topic: string
  target_audience: string
  platform: ContentPlatform
  style: string
  target_chars: number
  review_notes: string[]
  status: DraftStatus
  status_reason: string
  evidence_refs: string[]
  research_id: string
  policy_validity_status: PolicyValidity
  policy_validity_note: string
  region_applicability: string
  generation_mode: ContentMode
  model_name: string
  prompt_version: string
  token_usage: {
    input_tokens: number | null
    output_tokens: number | null
    total_tokens: number
    api_attempts: number
    known_total_tokens: number
    usage_incomplete: boolean
  }
  created_at: string
  schema_version: string
}

export interface DraftStatistics {
  key_points_total: number
  key_points_bound_to_evidence: number
  key_points_pending_semantic_review: number
  key_points_human_confirmed: number
  human_confirmation_available: boolean
  char_count: number
}

export interface ContentRun {
  task_id: string | null
  status_reason: string
  budget: {
    used_api_attempts: number
    max_api_attempts: number
    known_total_tokens: number
    usage_incomplete: boolean
  } | null
  generation_mode: ContentMode
  status: DraftStatus | 'draft_ready' | 'needs_manual_review' | 'budget_exhausted'
  evidence_package: EvidencePackage | null
  content_draft: ContentDraft | null
  research_statistics: ResearchStatistics | null
  draft_statistics: DraftStatistics | null
  review_report: {
    status: string
    fact_support_llm_used: boolean
    dimension_results: Array<{ dimension: string; verdict: string; detail: string }>
    issues: Array<{ issue_id: string; code: string; message: string }>
  } | null
  revision_records: Array<{
    revision_id: string
    status: string
    changed: boolean
    before_title: string
    before_body: string
    after_title: string
    after_body: string
  }>
}
