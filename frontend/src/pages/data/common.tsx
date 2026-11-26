import type { ReactNode } from 'react'
import { ArrowDownRight, ChevronLeft, ChevronRight, ExternalLink } from 'lucide-react'
import { officialLink } from '../../api/client'
import { Badge, ErrorState, Loading } from '../../components/ui'
import type { DataView } from './models'

type SearchValue = string | number | undefined

export function withQuery(path: string, entries: Record<string, SearchValue>) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(entries)) {
    if (value !== undefined && value !== '') query.set(key, String(value))
  }
  return query.size ? `${path}?${query}` : path
}

export function dataLink(view: DataView, entries: Record<string, SearchValue> = {}) {
  return withQuery(`#/data/${view}`, entries)
}

export function routeParam(key: string) {
  const search = location.hash.split('?')[1]
  return new URLSearchParams(search).get(key) ?? ''
}

const dateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

export function timeLabel(value?: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : dateTime.format(date)
}

export function sizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function OfficialLink({ url, children }: { url: string; children?: ReactNode }) {
  const href = officialLink(url)
  if (!href) return <span>{children ?? url}</span>
  return <a href={href} target="_blank" rel="noreferrer" className="dc-link">
    {children ?? new URL(href).hostname}<ExternalLink size={13} aria-hidden />
  </a>
}

export function Hash({ value, label }: { value?: string; label?: string }) {
  if (!value) return <span>—</span>
  return <code className="dc-hash" title={value} aria-label={label ? `${label} ${value}` : value}>{value}</code>
}

export function QueryState({ loading, error, reload, children }: {
  loading: boolean
  error: string | null
  reload: () => void
  children: ReactNode
}) {
  if (loading) return <Loading />
  if (error) return <ErrorState message={error} retry={reload} />
  return children
}

const statuses: Record<string, { label: string; tone: 'neutral' | 'success' | 'warning' | 'error' | 'accent' }> = {
  ok: { label: '成功', tone: 'success' },
  succeeded: { label: '成功', tone: 'success' },
  success: { label: '成功', tone: 'success' },
  completed: { label: '完成', tone: 'success' },
  failed: { label: '失败', tone: 'error' },
  error: { label: '失败', tone: 'error' },
  partial: { label: '部分成功', tone: 'warning' },
  pending: { label: '待审核', tone: 'warning' },
  pending_approval: { label: '待审核', tone: 'warning' },
  pending_review: { label: '待审核', tone: 'warning' },
  approved: { label: '已批准', tone: 'success' },
  skipped: { label: '已跳过', tone: 'neutral' },
  disabled: { label: '已停用', tone: 'neutral' },
}

export function ItemStatus({ state }: { state: string }) {
  const status = statuses[state]
  return <Badge tone={status?.tone ?? 'neutral'}>{status?.label ?? state}</Badge>
}

export function SectionHeading({ title, description, action }: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return <div className="dc-section-head">
    <div><h2>{title}</h2>{description && <p>{description}</p>}</div>{action}
  </div>
}

export function DataTable({ label, headers, children }: {
  label: string
  headers: string[]
  children: ReactNode
}) {
  return <div className="dc-table-scroll" tabIndex={0} role="region" aria-label={label}>
    <table className="dc-table" aria-label={label}>
      <thead><tr>{headers.map(header => <th key={header} scope="col">{header}</th>)}</tr></thead>
      <tbody>{children}</tbody>
    </table>
  </div>
}

export function PageControls({ offset, limit, total, onOffset }: {
  offset: number
  limit: number
  total: number
  onOffset: (offset: number) => void
}) {
  if (total <= limit) return null
  return <div className="dc-pager">
    <p>{offset + 1}–{Math.min(offset + limit, total)} / {total}</p>
    <div>
      <button className="dc-button" disabled={!offset} onClick={() => onOffset(Math.max(0, offset - limit))}>
        <ChevronLeft size={15} aria-hidden />上一页
      </button>
      <button className="dc-button" disabled={offset + limit >= total} onClick={() => onOffset(offset + limit)}>
        下一页<ChevronRight size={15} aria-hidden />
      </button>
    </div>
  </div>
}

export function RelatedLink({ view, query, children }: {
  view: DataView
  query?: Record<string, SearchValue>
  children: ReactNode
}) {
  return <a className="dc-button" href={dataLink(view, query)}>{children}<ArrowDownRight size={14} aria-hidden /></a>
}
