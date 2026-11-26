import {
  ArrowDownRight, ArrowRight, ArrowUpRight, Box, Braces, Check, Database, FileClock,
  FileText, GitBranch, Globe2, Network, Radio, RefreshCw, Terminal, Workflow, Zap,
} from 'lucide-react'
import { Badge, Empty, ErrorState, Loading, Panel } from '../components/ui'
import { useApi } from '../hooks/useApi'
import type { OverviewData, RunReport } from '../types/workbench'

const dateFormat = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
})

const pipeline = [
  { label: '数据源', detail: 'Registry', icon: Globe2, href: '/data/sources' },
  { label: '采集与解析', detail: 'Ingestion', icon: GitBranch, href: '/data/ingestion' },
  { label: '版本化存储', detail: 'SQLite', icon: Database, href: '/data/policies' },
  { label: '政策工具', detail: 'Tools', icon: Braces, href: '/tools' },
  { label: 'Agent 运行时', detail: 'Runtime', icon: Terminal, href: '/playground' },
]

const built = [
  ['运行时', 'LLM Client · Message Manager · Tool System'],
  ['运行保障', 'Agent Loop · Task Budget · Execution Trace'],
  ['数据采集', 'Source Registry · Fetcher · Parser · Pipeline'],
  ['政策服务', '版本化存储 · Policy Query · PDF 摄取 · 文档切片'],
]

function HeroDiagram() {
  return <div className="hero-visual" aria-hidden="true">
    <div className="hero-ring outer" /><div className="hero-ring inner" />
    <div className="hero-wire wire-left" /><div className="hero-wire wire-right" />
    <div className="hero-wire wire-top" /><div className="hero-wire wire-bottom" />
    <span className="orbit-node node-db"><Database size={19} /></span>
    <span className="orbit-node node-flow"><Workflow size={19} /></span>
    <span className="orbit-node node-api"><Braces size={18} /></span>
    <span className="orbit-node node-box"><Box size={18} /></span>
    <span className="hero-core"><Zap size={39} strokeWidth={1.7} /></span>
  </div>
}

function ReportRow({ run }: { run: RunReport }) {
  const date = run.started_at ? new Date(run.started_at) : null
  const succeeded = run.status && ['success', 'completed'].includes(run.status)
  const mode = ['replay', 'offline', 'offline_replay'].includes(run.mode) ? '离线重放' :
    ['live', 'online'].includes(run.mode) ? '在线采集' : run.mode
  return <a className="report-row" href={`#/data/ingestion?run_id=${encodeURIComponent(run.run_id)}`}>
    <span className="report-icon"><GitBranch size={15} aria-hidden="true" /></span>
    <span className="report-label"><strong>{mode}</strong>
      <small><code>{run.run_id.slice(0, 8)}</code> · {date && Number.isFinite(date.getTime()) ? dateFormat.format(date) : '时间未记录'}</small>
    </span>
    {run.status && <Badge tone={succeeded ? 'success' : 'warning'}>{succeeded ? '完成' : run.status}</Badge>}
    <ArrowUpRight className="report-arrow" size={14} aria-hidden="true" />
  </a>
}

export default function Overview() {
  const { data, loading, error, reload } = useApi<OverviewData>('/overview')
  const metrics = [
    { label: '注册数据源', count: data?.counts.sources, caption: 'Source Registry', icon: Globe2, href: '/data/sources', theme: 'blue' },
    { label: '在库政策', count: data?.counts.policies, caption: '政策身份', icon: FileText, href: '/data/policies', theme: 'cyan' },
    { label: '已存版本', count: data?.counts.versions, caption: data ? `其中历史版本 ${data.counts.historical_versions}` : '含当前及历史版本', icon: FileClock, href: '/data/versions', theme: 'neutral' },
    { label: '采集事件', count: data?.counts.fetches, caption: 'SQLite 归档记录', icon: Radio, href: '/data/fetches', theme: 'orange' },
  ]

  return <>
    <div className="overview-intro">
      <div><h1>项目总览</h1><p>查看政策数据、采集链路和 Agent 运行情况。</p></div>
      <button className="button refresh-button" onClick={reload} disabled={loading}>
        <RefreshCw size={15} aria-hidden="true" /> 刷新数据
      </button>
    </div>
    <section className="overview-hero" aria-labelledby="hero-title">
      <div className="hero-copy">
        <div className="hero-kicker"><span className="hero-phase"><i />第一阶段工作台</span>
          {data && <code>v{data.version}</code>}
        </div>
        <h2 id="hero-title">PowerCopy V2</h2>
        <p>面向电力政策研究的 Agent 工作台。<br className="hero-break" />从官方数据采集、版本化存储到可追溯的工具调用。</p>
        <div className="hero-actions">
          <a className="button button-primary" href="#/playground"><Zap size={15} aria-hidden="true" /> 调试 Agent <ArrowRight size={15} aria-hidden="true" /></a>
          <a className="button button-quiet" href="#/data/policies">浏览政策库 <ArrowUpRight size={15} aria-hidden="true" /></a>
        </div>
      </div>
      <HeroDiagram />
    </section>

    <div className="overview-section-title"><h2>数据中心概况</h2>{data && <span><Database size={13} aria-hidden="true" /><code>{data.data_source}</code></span>}</div>
    {error && <Panel><ErrorState message={error} retry={reload} /></Panel>}
    <div className="metric-grid" aria-busy={loading}>
      {metrics.map(({ label, count, caption, icon: Icon, href, theme }) =>
        <a href={`#${href}`} key={label} className={`metric-card metric--${theme}`}>
          <div className="metric-top"><span className="metric-icon"><Icon size={17} aria-hidden="true" /></span>
            <ArrowUpRight className="metric-arrow" size={15} aria-hidden="true" /></div>
          <div className="metric-value">{count ?? '—'}</div>
          <div className="metric-label">{label}</div><div className="metric-caption">{caption}</div>
        </a>)}
    </div>

    <div className="overview-grid">
      <Panel className="overview-panel architecture">
        <div className="panel-heading"><span className="panel-icon"><Network size={18} aria-hidden="true" /></span>
          <div><h2>系统架构</h2><p>从数据源到 Agent 的可追溯链路</p></div>
        </div>
        <ol className="architecture-flow">{pipeline.map(({ label, detail, icon: Icon, href }) => <li key={label}>
          <a href={`#${href}`}><span className="flow-icon"><Icon size={19} aria-hidden="true" /></span>
            <strong>{label}</strong><small>{detail}</small>
          </a>
        </li>)}</ol>
        <div className="architecture-footer"><ArrowDownRight size={14} aria-hidden="true" />
          <span>查询、版本、工具结果与 Trace 可关联核对</span>
          <a href="#/observability">查看可观测中心 <ArrowRight size={13} aria-hidden="true" /></a>
        </div>
      </Panel>
      <Panel className="overview-panel">
        <div className="panel-heading"><span className="panel-icon"><GitBranch size={18} aria-hidden="true" /></span>
          <div><h2>最近采集运行</h2><p>本地采集报告</p></div>
          <a className="panel-link" href="#/data/ingestion" aria-label="查看全部采集运行"><ArrowUpRight size={16} aria-hidden="true" /></a>
        </div>
        {loading && <Loading />}
        {data && (data.recent_runs.length ? data.recent_runs.map(run => <ReportRow key={run.run_id} run={run} />) :
          <Empty title="暂无采集报告" />)}
        {error && <Empty title="采集报告不可用" />}
      </Panel>
    </div>

    <div className="overview-grid">
      <Panel className="overview-panel">
        <div className="panel-heading"><span className="panel-icon"><Workflow size={18} aria-hidden="true" /></span>
          <div><h2>已实现研发阶段</h2><p>当前项目源码与已有数据产物</p></div>
        </div>
        <ul className="stage-list">{built.map(([label, detail]) => <li key={label}>
          <Check size={16} aria-hidden="true" /><strong>{label}</strong><span>{detail}</span>
        </li>)}</ul>
      </Panel>
      <Panel className="overview-panel">
        <div className="panel-heading"><span className="panel-icon"><Check size={18} aria-hidden="true" /></span>
          <div><h2>测试与验收</h2><p>本地实际执行结果</p></div>
        </div>
        {loading && <Loading />}
        {data && (data.acceptance.length ? <ul className="acceptance-list">{data.acceptance.map(suite => <li key={suite.label}>
          <div><strong>{suite.label}</strong>{suite.summary && <small>{suite.summary}</small>}</div>
          <Badge tone={suite.status === 'passed' ? 'success' : 'warning'}>
            {suite.status === 'passed' ? (suite.passed != null ? `${suite.passed} 项通过` : '通过') : suite.status}
          </Badge>
        </li>)}</ul> : <Empty title="暂无已导入的验收结果" />)}
        {error && <Empty title="验收结果不可用" />}
      </Panel>
    </div>
  </>
}
