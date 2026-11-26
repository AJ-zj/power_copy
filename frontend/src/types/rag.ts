export type SearchMode = 'lexical' | 'dense' | 'hybrid'
export type SearchPath = Exclude<SearchMode, 'hybrid'>
export type IndexAvailability = 'ready' | 'missing' | 'invalid'

export interface Freshness {
  status: string
  detail: string | null
}

export interface IndexState {
  status: IndexAvailability
  schema: string | null
  fingerprint: string | null
  manifest_fingerprint: string | null
  model: string | null
  model_commit: string | null
  dimension: number | null
}

export interface IndexOverview {
  documents: number | null
  chunks: number | null
  manifest_fingerprint: string | null
  freshness: Freshness
  indexes: Record<SearchPath, IndexState>
  consistency_status: string
}

export interface SearchRequest {
  query: string
  mode: SearchMode
  top_k: number
}

export interface PathResult {
  status: string
  match_mode: string | null
  returned: number | null
}

export interface PathContribution {
  path: SearchPath
  rank: number
  score: number | null
  match_mode: string | null
  rrf: number | null
}

export interface Candidate {
  rank: number
  chunk_id: string
  policy_id: string
  document_id: string
  version: number
  page: number | null
  start: number
  end: number
  title: string
  source_key: string
  source_url: string
  content_sha256: string
  text: string
  score: number
  contributions: PathContribution[]
}

export interface SearchResult extends SearchRequest {
  status: 'success' | 'empty' | 'degraded' | 'failed'
  code: string | null
  message: string | null
  elapsed_ms: number
  match_mode: string | null
  consistency_status: string
  freshness: Freshness
  degraded_status: string | null
  paths: Record<SearchPath, PathResult>
  candidates_are_not_facts: true
  candidates: Candidate[]
}

export interface BenchmarkMetrics {
  recall_at_k?: number
  mrr?: number
  ndcg_at_k?: number
  hit_rate?: number
  complete_evidence_rate?: number
  multi_evidence_complete_rate?: number
  by_type?: Record<string, BenchmarkMetrics>
  count?: number
  cases_total?: number
  cases_scored?: number
  empty_cases?: number
  empty_pass?: number
  multi_evidence_cases?: number
}

export interface BenchmarkMode {
  at_1?: BenchmarkMetrics
  at_5?: BenchmarkMetrics
  at_10?: BenchmarkMetrics
  timing_ms?: { avg?: number; min?: number; max?: number }
}

export interface BenchmarkReport {
  status: IndexAvailability
  eval_set: string | null
  eval_version: number | string | null
  evaluated_at: string | null
  report_modified_at: string | null
  corpus: { documents: number; chunks: number } | null
  kind: 'development'
  modes: Partial<Record<SearchMode, BenchmarkMode>>
}
