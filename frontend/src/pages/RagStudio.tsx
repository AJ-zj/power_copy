import { useState, type FormEvent, type ReactNode } from 'react'
import { Activity, AlertTriangle, ArrowUpRight, Database, Fingerprint, Search, SlidersHorizontal } from 'lucide-react'
import { officialLink, post } from '../api/client'
import { Badge, Empty, ErrorState, Loading, PageHeading, Panel } from '../components/ui'
import { useApi } from '../hooks/useApi'
import type { BenchmarkMetrics, BenchmarkReport, Candidate, IndexOverview, IndexState, SearchMode, SearchPath, SearchRequest, SearchResult } from '../types/rag'
import './RagStudio.css'

const modes = {
  lexical: '关键词 · BM25',
  dense: '向量 · BGE',
  hybrid: '混合 · RRF',
}
const allModes: SearchMode[] = ['lexical', 'dense', 'hybrid']
const paths: SearchPath[] = ['lexical', 'dense']
const statusLabels: Record<string, string> = {
  ready: '就绪', ok: '一致', success: '成功', empty: '无候选', degraded: '已降级',
  failed: '失败', error: '失败', missing: '缺失', invalid: '异常', not_run: '未执行',
  not_requested: '未请求', skipped: '未执行', unavailable: '不可用', stale: '索引可能过期', not_applicable: '不适用',
  single_path_unconfigured: '仅配置单路', fingerprint_missing: '缺少指纹',
  version_conflict: '索引版本冲突', lexical_failed: '关键词检索失败', dense_failed: '向量检索失败',
  single_path_failed: '单路检索失败', both_paths_failed: '双路检索失败', index_unavailable: '索引不可用',
  db_unavailable: '政策库不可用', db_read_error: '政策库读取异常',
  manifest_missing: '切片清单缺失', manifest_unreadable: '切片清单读取异常',
}
const queryTypes: Record<string, string> = {
  exact_keyword: '精确关键词', short_word: '短词', semantic: '语义',
  cross_clause: '跨条款', policy_relation: '政策关系表述',
}

const dateTime = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  hour12: false, timeZone: 'Asia/Shanghai',
})

function measured(value?: number | null, digits = 4) {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits)
}

function matchScore(value: number | null, digits: number) {
  if (value != null && value !== 0 && Math.abs(value) < 10 ** -digits) return value.toExponential(2)
  return measured(value, digits)
}

function titleFor(status: string) {
  return statusLabels[status] ?? status
}

function StateBadge({ status }: { status: string }) {
  const tone = ['success', 'ok', 'ready'].includes(status) ? 'success' :
    ['failed', 'invalid', 'error', 'version_conflict'].includes(status) ? 'error' :
    ['degraded', 'stale', 'missing', 'unavailable', 'fingerprint_missing'].includes(status) ? 'warning' : 'neutral'
  return <Badge tone={tone}>{titleFor(status)}</Badge>
}

function SectionHeading({ id, title, description, icon, action }: {
  id: string
  title: string
  description?: string
  icon: ReactNode
  action?: ReactNode
}) {
  return <header className="rag-section-head">
    <span className="panel-icon" aria-hidden="true">{icon}</span>
    <div><h2 id={id}>{title}</h2>{description && <p>{description}</p>}</div>
    {action && <div className="rag-section-action">{action}</div>}
  </header>
}

function IndexCard({ mode, index }: { mode: SearchPath; index: IndexState }) {
  return <article className="rag-card">
    <div className="rag-card-head"><h3>{modes[mode]}</h3><StateBadge status={index.status} /></div>
    <dl className="rag-meta">
      <div><dt>索引格式</dt><dd><code>{index.schema ?? '—'}</code></dd></div>
      <div><dt>索引指纹</dt><dd><code>{index.fingerprint ?? '—'}</code></dd></div>
      <div><dt>切片清单指纹</dt><dd><code>{index.manifest_fingerprint ?? '—'}</code></dd></div>
      {mode === 'dense' && <>
        <div><dt>嵌入模型</dt><dd>{index.model ?? '—'}</dd></div>
        <div><dt>模型版本</dt><dd><code>{index.model_commit ?? '—'}</code></dd></div>
        <div><dt>向量维度</dt><dd>{index.dimension ?? '—'}</dd></div>
      </>}
    </dl>
  </article>
}

function IndexStatus() {
  const { data, error, loading, reload } = useApi<IndexOverview>('/rag/status')
  return <Panel>
    <SectionHeading id="rag-index-heading" title="索引状态" icon={<Database size={17} />}
      action={<button type="button" className="button" onClick={reload} disabled={loading}>刷新索引状态</button>} />
    {loading && <Loading />}
    {error && <ErrorState message={error} retry={reload} />}
    {data && <>
      <dl className="rag-overview">
        <div><dt>政策文档</dt><dd>{data.documents ?? '—'}</dd></div>
        <div><dt>索引切片</dt><dd>{data.chunks ?? '—'}</dd></div>
        <div><dt>与政策库对照</dt><dd><StateBadge status={data.freshness.status} /></dd></div>
        <div><dt>两路索引一致性</dt><dd><StateBadge status={data.consistency_status} /></dd></div>
      </dl>
      {data.freshness.status !== 'ok' && data.freshness.detail && <p className="rag-callout"><AlertTriangle size={15} aria-hidden="true" />{data.freshness.detail}</p>}
      <div className="rag-indexes">{paths.map(mode => <IndexCard key={mode} mode={mode} index={data.indexes[mode]} />)}</div>
      {data.manifest_fingerprint && <div className="rag-manifest">
        <Fingerprint size={15} aria-hidden="true" /><span>当前切片清单</span><code>{data.manifest_fingerprint}</code>
      </div>}
    </>}
  </Panel>
}

function RetrievalDiagnostics({ result }: { result?: SearchResult }) {
  return <Panel>
    <SectionHeading id="rag-diagnostics-heading" title="检索诊断" icon={<Activity size={17} />}
      action={result && <StateBadge status={result.status} />} />
    {result ? <>
      <dl className="rag-meta rag-diagnostics">
        <div><dt>服务端耗时</dt><dd>{measured(result.elapsed_ms, 1)} ms</dd></div>
        <div><dt>匹配方式</dt><dd><code>{result.match_mode ?? '—'}</code></dd></div>
        <div><dt>索引一致性</dt><dd><StateBadge status={result.consistency_status} /></dd></div>
        <div><dt>与政策库对照</dt><dd><StateBadge status={result.freshness.status} /></dd></div>
      </dl>
      <div className="rag-paths">{paths.map(path => {
        const outcome = result.paths[path]
        return <div className="rag-path" key={path}>
          <strong>{modes[path]}</strong><StateBadge status={outcome.status} />
          {['success', 'empty'].includes(outcome.status) && <span>{outcome.returned ?? 0} 条</span>}
          {outcome.match_mode && <code>{outcome.match_mode}</code>}
        </div>
      })}</div>
      {result.degraded_status && <div className="rag-callout"><AlertTriangle size={15} aria-hidden="true" />
        <span>降级原因 · {titleFor(result.degraded_status)} <code>{result.degraded_status}</code></span>
      </div>}
      {result.freshness.status !== 'ok' && result.freshness.detail && <p className="rag-callout">{result.freshness.detail}</p>}
    </> : <Empty title="暂无检索记录" description="运行一次检索后，可在这里核对各路状态、耗时和降级原因。" />}
  </Panel>
}

function CandidateCard({ candidate, mode }: { candidate: Candidate; mode: SearchMode }) {
  const source = officialLink(candidate.source_url)
  const policyQuery = new URLSearchParams({ policy_id: candidate.policy_id, version: String(candidate.version) })
  return <li className="rag-candidate">
    <article>
      <div className="rag-candidate-head">
        <span className="rag-rank" aria-label={`第 ${candidate.rank} 名`}>#{candidate.rank}</span>
        <h3>{candidate.title}</h3>
        <Badge tone="accent">{mode === 'hybrid' ? 'RRF' : mode === 'dense' ? '向量分数' : '关键词分数'} {matchScore(candidate.score, 6)}</Badge>
      </div>
      <div className="rag-candidate-meta">
        <span>v{candidate.version}</span>
        <span>原文区间 [{candidate.start}, {candidate.end})</span>
        {candidate.page != null && <span>第 {candidate.page} 页</span>}
        <span>来源 <code>{candidate.source_key}</code></span>
      </div>
      <details className="rag-excerpt">
        <summary>查看片段和检索贡献</summary>
        <blockquote>{candidate.text}</blockquote>
        <div className="rag-contributions">
          {candidate.contributions.map(contribution => <div key={contribution.path}>
            <strong>{modes[contribution.path]}</strong>
            <span>排名 #{contribution.rank}</span><span>原始分数 {matchScore(contribution.score, 6)}</span>
            {contribution.rrf != null && <span>RRF 贡献 {matchScore(contribution.rrf, 8)}</span>}
            {contribution.match_mode && <code>{contribution.match_mode}</code>}
          </div>)}
        </div>
        <dl className="rag-meta rag-identifiers">
          <div><dt>切片 ID</dt><dd><code>{candidate.chunk_id}</code></dd></div>
          <div><dt>文档 ID</dt><dd><code>{candidate.document_id}</code></dd></div>
          <div><dt>正文哈希</dt><dd><code>{candidate.content_sha256}</code></dd></div>
        </dl>
      </details>
      <div className="rag-candidate-links">
        <a className="button button-quiet" href={'#/data/policies?' + policyQuery}>查看政策原文</a>
        {source && <a className="button button-quiet" href={source} target="_blank" rel="noreferrer">访问官方来源<ArrowUpRight size={13} aria-hidden="true" /></a>}
      </div>
    </article>
  </li>
}

type BenchmarkCutoff = 'at_1' | 'at_5' | 'at_10'
const metricFields = [
  ['recall_at_k', '召回率'], ['hit_rate', '命中率'], ['mrr', 'MRR'], ['ndcg_at_k', 'nDCG'],
  ['complete_evidence_rate', '完整证据率'], ['multi_evidence_complete_rate', '多证据完整率'],
] as const

function BenchmarkCells({ values }: { values?: BenchmarkMetrics }) {
  return metricFields.map(([field]) => <td key={field}>{measured(values?.[field])}</td>)
}

function Benchmark() {
  const request = useApi<BenchmarkReport>('/rag/benchmark')
  const [cutoff, setCutoff] = useState<BenchmarkCutoff>('at_10')
  const data = request.data
  const rawDate = data?.evaluated_at ?? data?.report_modified_at
  const date = rawDate ? new Date(rawDate) : null
  return <Panel>
    <SectionHeading id="rag-benchmark-heading" title="开发评测基准" icon={<SlidersHorizontal size={17} />}
      description="指标来自已有开发评测产物。业务验收需使用独立样本。" />
    {request.loading && <Loading />}
    {request.error && <ErrorState message={request.error} retry={request.reload} />}
    {data && data.status !== 'ready' && <Empty title={data.status === 'missing' ? '暂无开发评测报告' : '开发评测报告无法读取'} />}
    {data?.status === 'ready' && <>
      <dl className="rag-meta rag-benchmark-meta">
        <div><dt>评测集</dt><dd>{data.eval_set ?? '—'}{data.eval_version != null && ` · v${data.eval_version}`}</dd></div>
        <div><dt>评测语料</dt><dd>{data.corpus ? `${data.corpus.documents} 份文档 · ${data.corpus.chunks} 个切片` : '—'}</dd></div>
        <div><dt>{data.evaluated_at ? '评测时间（北京时间）' : '报告文件修改时间（北京时间）'}</dt>
          <dd>{date && Number.isFinite(date.getTime()) ? dateTime.format(date) : '—'}</dd></div>
      </dl>
      <div className="rag-cutoff"><label htmlFor="rag-benchmark-cutoff">评测截断位置</label>
        <select className="field" id="rag-benchmark-cutoff" value={cutoff} onChange={event => setCutoff(event.target.value as BenchmarkCutoff)}>
          <option value="at_1">Top 1</option><option value="at_5">Top 5</option><option value="at_10">Top 10</option>
        </select>
      </div>
      <div className="rag-table-scroll" role="region" aria-label="开发评测指标" tabIndex={0}>
        <table className="rag-table">
          <caption>三种检索模式的离线开发评测</caption>
          <thead><tr><th scope="col">模式</th>{metricFields.map(([field, label]) => <th scope="col" key={field}>{label}</th>)}
            <th scope="col">空结果通过数</th><th scope="col">平均耗时（ms）</th></tr></thead>
          <tbody>{allModes.map(mode => {
            const values = data.modes[mode]?.[cutoff]
            return <tr key={mode}><th scope="row">{modes[mode]}</th>
              <BenchmarkCells values={values} />
              <td>{values?.empty_pass != null && values?.empty_cases != null ? `${values.empty_pass} / ${values.empty_cases}` : '—'}</td>
              <td>{measured(data.modes[mode]?.timing_ms?.avg, 2)}</td>
            </tr>
          })}</tbody>
        </table>
      </div>
      <p className="rag-benchmark-note">耗时沿用原报告的模式平均值。横线表示报告缺少该项指标。</p>
      {allModes.map(mode => {
        const metrics = data.modes[mode]?.[cutoff]
        if (!metrics?.by_type || !Object.keys(metrics.by_type).length) return null
        return <details className="rag-excerpt rag-benchmark-details" key={mode}>
          <summary>{modes[mode]} · 分类型结果</summary>
          <div className="rag-table-scroll" role="region" aria-label={modes[mode] + ' 分类型指标'} tabIndex={0}>
            <table className="rag-table">
              <caption>{modes[mode]} 分类型指标</caption>
              <thead><tr><th scope="col">题型</th><th scope="col">样本数</th><th scope="col">召回率</th>
                <th scope="col">命中率</th><th scope="col">完整证据率</th></tr></thead>
              <tbody>{Object.entries(metrics.by_type).map(([type, values]) => <tr key={type}>
                <th scope="row">{queryTypes[type] ?? type}</th><td>{values.count ?? '—'}</td>
                <td>{measured(values.recall_at_k)}</td><td>{measured(values.hit_rate)}</td><td>{measured(values.complete_evidence_rate)}</td>
              </tr>)}</tbody>
            </table>
          </div>
        </details>
      })}
    </>}
  </Panel>
}

export default function RagStudio() {
  const [pending, setPending] = useState(false)
  const [failure, setFailure] = useState<{ message: string; request: SearchRequest }>()
  const [result, setResult] = useState<SearchResult>()

  async function search(request: SearchRequest) {
    setPending(true)
    setFailure(undefined)
    setResult(undefined)
    try {
      setResult(await post<SearchResult>('/rag/search', request))
    } catch (error) {
      setFailure({ message: error instanceof Error ? error.message : '检索请求失败。', request })
    } finally {
      setPending(false)
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending) return
    const values = new FormData(event.currentTarget)
    const query = String(values.get('query')).trim()
    if (!query) {
      const input = event.currentTarget.elements.namedItem('query') as HTMLInputElement
      input.setCustomValidity('请输入检索问题。')
      input.reportValidity()
      return
    }
    void search({ query, mode: values.get('mode') as SearchMode, top_k: Number(values.get('top_k')) })
  }

  return <>
    <PageHeading eyebrow="POLICY RETRIEVAL" title="RAG Studio" description="对比政策检索模式，查看索引状态，并回溯候选片段的政策原文。" />
    <div className="rag-studio">
      <IndexStatus />
      <div className="rag-search-grid">
        <Panel>
          <SectionHeading id="rag-search-heading" title="检索调试" icon={<Search size={17} />} />
          <search aria-labelledby="rag-search-heading">
            <form className="rag-form" onSubmit={submit}>
              <label htmlFor="rag-query">检索问题</label>
              <input type="search" className="field" required maxLength={200} id="rag-query" name="query"
                placeholder="例如 电力市场主体需要哪些注册材料" enterKeyHint="search"
                onInput={event => event.currentTarget.setCustomValidity('')} />
              <div className="rag-controls">
                <div><label htmlFor="rag-mode">检索模式</label><select className="field" name="mode" id="rag-mode" defaultValue="hybrid">
                  {allModes.map(mode => <option key={mode} value={mode}>{modes[mode]}</option>)}
                </select></div>
                <div><label htmlFor="rag-top-k">Top K</label><input type="number" name="top_k" className="field" id="rag-top-k" min={1} max={20} defaultValue={5} required /></div>
              </div>
              <button type="submit" className="button button-primary" disabled={pending}><Search size={15} aria-hidden="true" />{pending ? '检索中…' : '运行检索'}</button>
            </form>
          </search>
        </Panel>
        <RetrievalDiagnostics result={result} />
      </div>
      <Panel>
        <SectionHeading id="rag-candidates-heading" title="候选证据" icon={<Fingerprint size={17} />}
          action={result && <Badge tone="accent">{result.candidates.length} 条候选</Badge>} />
        <div className="rag-candidates" aria-busy={pending}>
          <p className="rag-callout"><AlertTriangle size={15} aria-hidden="true" />检索结果为候选片段，引用前请核对政策原文、适用范围和有效性。</p>
          {pending && <Loading />}
          {failure && <ErrorState message={failure.message} retry={() => void search(failure.request)} />}
          {result?.message && <p className="rag-outcome" role={result.status === 'failed' ? 'alert' : undefined}>
            {result.message}{result.code && <> <code>{result.code}</code></>}
          </p>}
          {result?.status === 'failed' && !result.message && <p className="rag-outcome" role="alert">检索失败。</p>}
          <div className="rag-sr-only" role="status">{result && `共返回 ${result.candidates.length} 条候选。`}</div>
          {result && result.candidates.length > 0 && <ol role="list" className="rag-candidate-list">
            {result.candidates.map(candidate => <CandidateCard key={candidate.chunk_id} candidate={candidate} mode={result.mode} />)}
          </ol>}
          {result && result.candidates.length === 0 && result.status !== 'failed' && <Empty title="没有返回候选证据" />}
          {!result && !pending && !failure && <Empty title="在检索调试中提交问题" />}
        </div>
      </Panel>
      <Benchmark />
    </div>
  </>
}
