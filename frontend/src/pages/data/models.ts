export type DataView = 'sources' | 'ingestion' | 'policies' | 'versions' | 'fetches' | 'archive'

export interface Collection<T> {
  items: T[]
}

export interface Page<T> extends Collection<T> {
  total: number
}

export interface Source {
  source_key: string
  name: string
  allowed_host: string
  enabled: boolean
  targets: {
    target_key: string
    url: string
    content_kind: string
    approved: boolean
  }[]
}

export interface Candidate {
  source_key: string
  index_fetch_id: string
  url: string
  title: string
  status: string
}

export interface Sources extends Collection<Source> {
  candidates: Candidate[]
}

export interface Policy {
  policy_id: string
  version: number
  document_id: string
  content_sha256: string
  title: string
  region: string | null
  issuer: string | null
  published_on: string | null
  source_key: string
  source_url: string
  document_retrieved_at: string
  fetch_id: string
  raw_sha256: string
  last_observed_at: string
  observation_count: number
  is_latest_observed: boolean
  body_length: number
}

export interface Policies extends Page<Policy> {
  regions: string[]
  unassigned: number
  notice: string
}

export interface PolicyDetail extends Policy {
  body_text: string
  body_offset: number
  next_offset: number | null
  notice: string
}

export interface PolicyDiff {
  from_version: number
  to_version: number
  changes: { kind: 'equal' | 'delete' | 'insert'; text: string }[]
  metadata: { field: string; before: string | null; after: string | null }[]
}

export interface RunSummary {
  run_id: string
  mode: string
  status: string
  started_at?: string
  finished_at?: string
  requests: number
  items_total: number
  succeeded: number
  failed: number
}

export interface Run extends RunSummary {
  items: {
    source_key?: string
    target_key?: string
    stage: string
    status: string
    attempts?: number | { number: number; status: string; code?: string; http_status?: number }[]
    fetch_id?: string
    raw_path?: string
    error_type?: string
    error_code?: string
    result?: unknown
  }[]
  isolated?: boolean
}

export interface FetchRecord {
  fetch_id: string
  policy_id: string
  version: number
  retrieved_at: string
  raw_sha256: string
  document_id: string
  title?: string
}

export interface ArchiveItem {
  fetch_id: string
  source_key: string
  target_key: string
  source_url: string
  content_kind: string
  content_type: string
  retrieved_at: string
  body_size: number
  raw_sha256: string
  filename: string
  preview_kind: string | null
  replayable: boolean
  provenance: string
}

export interface Archive extends Collection<ArchiveItem> {
  unavailable: { fetch_id: string; code: string; message: string }[]
}
