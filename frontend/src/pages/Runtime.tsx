import { useState, type FormEvent } from 'react'
import { Activity, ArrowUpRight, Braces, ChevronRight, Play, Terminal } from 'lucide-react'
import { officialLink, post } from '../api/client'
import { Badge, Empty, ErrorState, Loading, PageHeading, Panel } from '../components/ui'
import { useApi } from '../hooks/useApi'
import type { Limits, RegisteredTool, RuntimeInfo, TaskResult } from '../types/workbench'

const limits = [
  { key: 'max_rounds', label: '最大轮次', max: 12 },
  { key: 'max_tool_calls', label: '工具次数上限', max: 24 },
  { key: 'max_api_attempts', label: '模型尝试上限', max: 24 },
  { key: 'max_known_tokens', label: '已知 Token 阈值', max: 200000 },
] as const

const names: Record<string, string> = {
  task_started: '任务开始', round_started: '开始执行轮次', model_attempt: '模型调用',
  model_response: '模型响应', tool_execution: '工具执行', task_completed: '响应生成完成',
  task_finished: '任务结束', task_error: '任务异常', retry_scheduled: '计划重试',
  model_retry: '模型重试',
  budget_exceeded: '预算耗尽', incomplete_response: '模型响应不完整',
  completed: '完成', success: '成功', failed: '失败', stopped: '已停止', unavailable: '无业务结果',
}

function Status({ value }: { value: string }) {
  const tone = ['success', 'completed'].includes(value) ? 'success' :
    value === 'failed' ? 'error' : value === 'unavailable' ? 'neutral' : 'warning'
  return <Badge tone={tone}>{names[value] ?? value}</Badge>
}

function Json({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2) ?? '—'}</pre>
}

function Metrics({ result }: { result: TaskResult }) {
  const stats = [
    ['执行轮次', `${result.rounds} / ${result.limits.max_rounds}`],
    ['模型尝试', `${result.model_calls} / ${result.budget.max_api_attempts}`],
    ['外部模型调用', result.external_model_calls],
    ['工具调用', `${result.tool_requests} / ${result.limits.max_tool_calls}`],
    ['已知 Token / 阈值', `${result.usage.known_total_tokens} / ${result.budget.max_known_tokens}`],
    ['Token 输入 / 输出', `${result.usage.input_tokens ?? '未知'} / ${result.usage.output_tokens ?? '未知'}`],
    ['Token 总量', result.usage.total_tokens ?? '未知'],
  ]
  return <dl className="runtime-metrics">{stats.map(([label, value]) =>
    <div key={label} className={label === '已知 Token / 阈值' ? 'metric-budget' : undefined}><dt>{label}</dt><dd>{value}</dd></div>,
  )}</dl>
}

function Result({ result }: { result: TaskResult }) {
  return <div className="runtime-results">
    <Panel className="runtime-panel">
      <div className="runtime-title"><h2>任务结果</h2><Status value={result.status} />
        <Badge tone={result.mode === 'local' ? 'accent' : 'warning'}>{result.mode === 'local' ? 'Mock LLM · 真实政策工具' : '真实模型'}</Badge>
      </div>
      <p className="runtime-id"><code>{result.task_id}</code> <a href="#/observability">查看 Trace <ArrowUpRight size={13} /></a></p>
      {result.content && <div className="runtime-response">{result.content}</div>}
      {result.failure && <div className="runtime-failure" role="alert">{result.failure.message} <code>{result.failure.code}</code></div>}
      <Metrics result={result} />
      {result.usage.usage_incomplete && <p className="muted">当前响应的 Token 用量记录不完整。</p>}
    </Panel>
    <Panel className="runtime-panel">
      <div className="runtime-title"><h2>工具调用</h2><Badge>{result.tool_calls.length} 次</Badge></div>
      {result.tool_calls.length ? <div className="runtime-details">{result.tool_calls.map((call, index) =>
        <details key={index}>
          <summary><ChevronRight size={15} aria-hidden="true" /><code>{call.name}</code>
            <span className="call-status">执行 <Status value={call.execution_status} /></span>
            <span className="call-status">业务 <Status value={call.business_status} /></span>
          </summary>
          <h3>调用参数</h3><Json value={call.arguments} />
          <h3>执行结果</h3>{call.error_message && <p className="runtime-failure">{call.error_message}</p>}<Json value={call.result} />
        </details>,
      )}</div> : <Empty title="该任务没有执行工具" />}
    </Panel>
    <Panel className="runtime-panel">
      <div className="runtime-title"><h2>政策来源与证据</h2><Badge>{result.evidence.length} 条</Badge></div>
      {result.evidence.length ? <ul className="evidence-list">{result.evidence.map(evidence => {
        const url = officialLink(evidence.source_url)
        return <li key={`${evidence.policy_id}-${evidence.version}`}>
          <div className="runtime-title"><strong>{evidence.title}</strong><Badge>v{evidence.version}</Badge></div>
          {evidence.snippet && <p>{evidence.snippet}</p>}
          {evidence.content_sha256 && <small className="muted">Content Hash <code>{evidence.content_sha256}</code></small>}
          <div className="runtime-links">
            <a href={`#/data/policies?policy_id=${encodeURIComponent(evidence.policy_id)}&version=${evidence.version}`}>在库详情</a>
            {url && <a href={url} target="_blank" rel="noopener noreferrer">官方来源 <ArrowUpRight size={13} /></a>}
          </div>
        </li>
      })}</ul> : <Empty title="本次任务没有返回政策证据" />}
    </Panel>
  </div>
}

export function Playground({ result, setResult }: { result: TaskResult | null; setResult: (result: TaskResult) => void }) {
  const config = useApi<RuntimeInfo>('/runtime')
  const [task, setTask] = useState('')
  const [keyword, setKeyword] = useState('')
  const [region, setRegion] = useState('')
  const [mode, setMode] = useState<'local' | 'live'>('local')
  const [confirmed, setConfirmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  async function run(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || !config.data || !task.trim()) return
    const fields = new FormData(event.currentTarget)
    const budget = Object.fromEntries(limits.map(({ key }) => [key, Number(fields.get(key))])) as unknown as Limits
    setPending(true)
    setError('')
    try {
      setResult(await post<TaskResult>('/runs', { task, keyword, region, mode, confirm_live: confirmed, ...budget }))
    } catch (error) {
      setError(error instanceof Error ? error.message : '任务请求失败。')
    } finally {
      setPending(false)
    }
  }

  return <>
    <PageHeading eyebrow="AGENT RUNTIME" title="Agent Playground"
      description="提交任务，查看工具执行、政策证据和预算消耗。">
      <a className="button" href="#/tools"><Braces size={16} aria-hidden="true" /> Tool Registry</a>
    </PageHeading>
    <div className="runtime-grid">
      <Panel className="runtime-panel runtime-form">
        <div className="runtime-title"><h2>新建调试任务</h2><Terminal size={17} aria-hidden="true" /></div>
        {config.loading && <Loading />}
        {config.error && <ErrorState message={config.error} retry={config.reload} />}
        {config.data && <form onSubmit={run}>
          <label>运行模式
            <select className="field" value={mode} onChange={event => {
              setMode(event.target.value === 'live' ? 'live' : 'local')
              setConfirmed(false)
            }}>
              <option value="local">本地 · Mock LLM</option>
              <option value="live" disabled={!config.data.live_enabled}>真实模型{config.data.live_enabled ? '' : ' · 服务端未启用'}</option>
            </select>
          </label>
          <p className="muted">{mode === 'local' ? 'Mock LLM 驱动现有 Agent。政策工具读取真实 SQLite。' : '本次任务会向服务端配置的模型提供商发送请求。'}</p>
          <label>任务描述<textarea className="field" rows={5} required maxLength={4000}
            value={task} onChange={event => setTask(event.target.value)} placeholder="例如，查询电力市场运行规则并核对官方来源" /></label>
          {mode === 'local' && <div className="form-pair">
            <label>检索关键词<input className="field" maxLength={200} value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="例如 电力" /></label>
            <label>地区<input className="field" maxLength={80} value={region} onChange={event => setRegion(event.target.value)} placeholder="所有地区" /></label>
          </div>}
          <fieldset><legend>运行预算</legend><div className="form-pair">
            {limits.map(({ key, label, max }) => <label key={key}>{label}
              <input className="field" name={key} type="number" min={1} max={max} required defaultValue={config.data?.budget[key]} />
            </label>)}
          </div></fieldset>
          {mode === 'live' && <label className="live-confirm"><input type="checkbox" required checked={confirmed}
            onChange={event => setConfirmed(event.target.checked)} />授权本次任务调用真实模型</label>}
          <button className="button button-primary" type="submit" disabled={pending || !task.trim() || (mode === 'live' && !confirmed)}>
            <Play size={15} aria-hidden="true" />{pending ? '任务执行中…' : '运行任务'}
          </button>
        </form>}
      </Panel>
      <div aria-busy={pending}>
        {pending && <Panel><div className="state-block" role="status">正在等待 Agent 返回完整结果…</div></Panel>}
        {error && <Panel><ErrorState message={error} /></Panel>}
        {result ? <Result result={result} /> : !pending && !error &&
          <Panel><Empty title="等待运行" description="任务完成后，结果、工具调用和政策证据将显示在这里。" /></Panel>}
      </div>
    </div>
  </>
}

export function Observability({ result }: { result: TaskResult | null }) {
  return <>
    <PageHeading eyebrow="EXECUTION TRACE" title="可观测中心" description="查看当前会话最近一次任务的执行轨迹。">
      <a className="button" href="#/playground"><Activity size={16} aria-hidden="true" /> Agent Playground</a>
    </PageHeading>
    {!result ? <Panel><Empty title="暂无当前任务轨迹" description="在 Agent Playground 执行任务后，可在此查看模型、工具和预算事件。" /></Panel> :
      <div className="runtime-results">
        <Panel className="runtime-panel">
          <div className="runtime-title"><h2>任务状态</h2><Status value={result.trace.status} />
            <Badge>{result.mode === 'local' ? 'Mock LLM' : '真实模型'}</Badge></div>
          <p className="runtime-id">Task ID <code>{result.trace.task_id}</code></p>
          <p className="muted">{new Date(result.trace.started_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} · {result.trace.duration_ms.toFixed(1)} ms · {result.trace.stop_reason}</p>
          <Metrics result={result} />
        </Panel>
        <Panel className="runtime-panel">
          <div className="runtime-title"><h2>运行事件时间线</h2><Badge>{result.trace.events.length} 条</Badge></div>
          <ol className="trace-list">{result.trace.events.map(event => <li key={event.event_id}>
            <details>
              <summary><span className="trace-offset">+{event.offset_ms.toFixed(1)} ms</span>
                <strong>{event.event_type === 'model_attempt' && event.is_retry ? '模型重试' : names[event.event_type] ?? event.event_type}</strong>
                {typeof event.attempt === 'number' && event.attempt > 1 && <span>尝试 {event.attempt}</span>}
                {event.tool_name && <code>{event.tool_name}</code>}
                {typeof event.round === 'number' && <span>第 {event.round} 轮</span>}
                {event.status && <Status value={event.status} />}
                {event.error_type && <code>{event.error_type}</code>}
              </summary>
              <Json value={event} />
            </details>
          </li>)}</ol>
        </Panel>
      </div>}
  </>
}

export function ToolsPage() {
  const tools = useApi<{ items: RegisteredTool[] }>('/tools')
  return <>
    <PageHeading eyebrow="TOOL SYSTEM" title="Tool Registry" description="查看当前运行时真实注册的工具、参数契约和使用限制。" />
    {tools.loading && <Panel><Loading /></Panel>}
    {tools.error && <Panel><ErrorState message={tools.error} retry={tools.reload} /></Panel>}
    {tools.data?.items.length === 0 && <Panel><Empty title="当前未注册工具" /></Panel>}
    <div className="tool-grid">{tools.data?.items.map(tool => <Panel className="runtime-panel" key={tool.name}>
      <div className="runtime-title"><Braces size={18} aria-hidden="true" /><h2><code>{tool.name}</code></h2></div>
      <p>{tool.description}</p>
      <div className="tool-limits">{tool.limitations.map(limit => <Badge key={limit}>{limit}</Badge>)}</div>
      <h3>参数 Schema</h3><Json value={tool.parameters} />
    </Panel>)}</div>
  </>
}
