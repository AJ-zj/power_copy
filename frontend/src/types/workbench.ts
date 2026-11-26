export interface RunReport {
  run_id: string
  mode: string
  status?: string
  requests?: number
  started_at?: string
  finished_at?: string
  items_total?: number
}

export interface Acceptance {
  label: string
  status: string
  command?: string
  passed?: number
  failed?: number
  summary?: string
}

export interface OverviewData {
  version: string
  counts: { sources: number; policies: number; versions: number; historical_versions: number; fetches: number; documents: number }
  recent_runs: RunReport[]
  data_source: string
  acceptance: Acceptance[]
}

export interface Limits {
  max_rounds: number
  max_tool_calls: number
  max_api_attempts: number
  max_known_tokens: number
}

export interface RuntimeInfo {
  local_available: boolean
  live_enabled: boolean
  streaming: boolean
  history: boolean
  budget: Limits
}

export interface TraceEvent {
  event_id: number
  event_type: string
  offset_ms: number
  status?: string
  round?: number
  tool_name?: string
  error_type?: string
  duration_ms?: number
  reason?: string
  stop_reason?: string
  [key: string]: unknown
}

export interface ToolCall {
  name: string
  arguments: unknown
  execution_status: string
  business_status: string
  result: unknown
  error_type?: string
  error_message?: string
  latency_ms?: number
}

export interface Evidence {
  policy_id: string
  version: number
  title: string
  source_url?: string
  source_key?: string
  fetch_id?: string
  content_sha256?: string
  kind?: string
  snippet?: string
}

export interface TaskResult {
  mode: 'local' | 'live'
  task_id: string
  status: string
  content: string | null
  rounds: number
  tool_requests: number
  model_calls: number
  external_model_calls: number
  budget: {
    api_attempts: number; max_api_attempts: number; remaining_api_attempts: number
    known_total_tokens: number; max_known_tokens: number; usage_incomplete: boolean
  }
  limits: Limits
  usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null; known_total_tokens: number; usage_incomplete: boolean }
  trace: { task_id: string; status: string; stop_reason: string; started_at: string; duration_ms: number; events: TraceEvent[] }
  tool_calls: ToolCall[]
  evidence: Evidence[]
  failure: { code: string; message: string } | null
}

export interface RegisteredTool {
  name: string
  description: string
  parameters: Record<string, unknown>
  limitations: string[]
}
