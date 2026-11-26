import { useState } from 'react'
import { ArrowRight, Archive, Database, FileSearch, Files, Globe2, ListChecks, Play, ShieldCheck } from 'lucide-react'
import { post } from '../../api/client'
import { Badge, Empty, ErrorState, PageHeading, Panel } from '../../components/ui'
import { useApi } from '../../hooks/useApi'
import { dataLink, DataTable, ItemStatus, QueryState, routeParam, SectionHeading, timeLabel } from './common'
import type { ArchiveItem, Collection, Run, RunSummary } from './models'

const stages = [
  { name: 'Source Registry', label: '已注册目标', icon: Globe2 },
  { name: 'Fetcher', label: '读取与抓取', icon: FileSearch },
  { name: 'Raw Storage', label: '保存原始内容', icon: Archive },
  { name: 'Parser', label: '解析正文', icon: Files },
  { name: 'Validator', label: '校验文档', icon: ShieldCheck },
  { name: 'VersionedPolicyStore', label: '管理内容版本', icon: ListChecks },
  { name: 'SQLite', label: '记录采集与版本', icon: Database },
]

const modes: Record<string, string> = {
  offline: '离线',
  offline_replay: '离线重放',
  online: '联网',
  live: '联网',
  replay: '离线重放',
}

const stageLabels: Record<string, string> = {
  authorize: '目标授权',
  fetch: '抓取',
  load_raw: '读取原始档案',
  save_raw: '保存原始档案',
  process: '内容处理',
}

function RunReport({ report }: { report: Run }) {
  return <Panel>
    <SectionHeading title={report.isolated ? '本次离线重放' : '运行详情'} description={report.run_id}
      action={<ItemStatus state={report.status} />} />
    <dl className="dc-stat-strip">
      <div><dt>目标记录</dt><dd>{report.items_total}</dd></div>
      <div><dt>成功</dt><dd>{report.succeeded}</dd></div>
      <div><dt>失败</dt><dd>{report.failed}</dd></div>
      <div><dt>网络请求</dt><dd>{report.requests}</dd></div>
    </dl>
    {report.items.length ? <DataTable label="采集运行各阶段结果" headers={['采集目标', '阶段', '状态', '尝试次数', '结果及失败原因']}>
      {report.items.map((item, index) => <tr key={`${item.target_key}-${item.stage}-${index}`}>
        <th scope="row"><strong>{item.target_key || item.raw_path || '—'}</strong>{item.source_key && <small>{item.source_key}</small>}</th>
        <td>{stageLabels[item.stage] ?? item.stage}<small><code>{item.stage}</code></small></td>
        <td><ItemStatus state={item.status} /></td>
        <td>{Array.isArray(item.attempts) ? item.attempts.length : item.attempts ?? '—'}</td>
        <td>
          {item.error_code && <code className="dc-failure">{item.error_code}</code>}
          {item.error_type && <small>{item.error_type}</small>}
          {item.fetch_id && <a className="dc-link" href={dataLink('archive', { fetch_id: item.fetch_id })}>查看原始档案</a>}
          {item.result != null && <details className="dc-details"><summary>阶段结果</summary><pre>{JSON.stringify(item.result, null, 2)}</pre></details>}
          {Array.isArray(item.attempts) && item.attempts.length > 0 && <details className="dc-details"><summary>请求尝试</summary><pre>{JSON.stringify(item.attempts, null, 2)}</pre></details>}
          {!item.error_code && !item.error_type && !item.fetch_id && item.result == null && '—'}
        </td>
      </tr>)}
    </DataTable> : <Empty title="本次运行没有阶段记录" />}
  </Panel>
}

export default function IngestionPage() {
  const reports = useApi<Collection<RunSummary>>('/ingestion/runs')
  const archive = useApi<Collection<ArchiveItem>>('/archive')
  const [selected, setSelected] = useState(routeParam('run_id'))
  const [fetchIds, setFetchIds] = useState<string[]>([])
  const [replay, setReplay] = useState<Run>()
  const [running, setRunning] = useState(false)
  const [replayError, setReplayError] = useState<string | null>(null)
  const runId = selected || reports.data?.items[0]?.run_id
  const detail = useApi<Run>(runId ? `/ingestion/runs/${encodeURIComponent(runId)}` : null)
  const replayable = archive.data?.items.filter(item => item.replayable) ?? []

  async function runReplay() {
    setRunning(true)
    setReplayError(null)
    setReplay(undefined)
    try {
      setReplay(await post<Run>('/ingestion/replay', { fetch_ids: fetchIds }))
    } catch (error) {
      setReplayError(error instanceof Error ? error.message : '离线重放失败')
    } finally {
      setRunning(false)
    }
  }

  return <>
    <PageHeading eyebrow="Ingestion Pipeline" title="采集流水线" description="查看已有运行报告与分阶段结果，使用登记档案执行离线重放。" />
    <div className="dc-stack">
      <Panel>
        <SectionHeading title="流水线架构" />
        <ol className="dc-flow" aria-label="数据采集流水线">
          {stages.map(({ name, label, icon: Icon }, index) => <li key={name}>
            <span className="dc-icon"><Icon size={17} aria-hidden /></span>
            <strong>{name}</strong><small>{label}</small>
            {index < stages.length - 1 && <ArrowRight className="dc-flow-arrow" size={15} aria-hidden />}
          </li>)}
        </ol>
      </Panel>
      <Panel>
        <SectionHeading title="已有运行报告" />
        <QueryState {...reports}>
          {reports.data?.items.length ? <DataTable label="采集运行报告" headers={['运行 ID', '模式', '结束时间（北京时间）', '网络请求', '成功 / 失败', '状态']}>
            {reports.data.items.map(item => <tr key={item.run_id} data-selected={runId === item.run_id}>
              <th scope="row"><button className="dc-text-button" aria-pressed={runId === item.run_id}
                onClick={() => setSelected(item.run_id)}><code>{item.run_id}</code></button></th>
              <td>{modes[item.mode] ?? item.mode}</td>
              <td>{timeLabel(item.finished_at ?? item.started_at)}</td>
              <td>{item.requests}</td>
              <td>{item.succeeded} / {item.failed}</td>
              <td><ItemStatus state={item.status} /></td>
            </tr>)}
          </DataTable> : <Empty title="暂无采集运行报告" />}
        </QueryState>
      </Panel>
      {runId && <QueryState {...detail}>{detail.data && <RunReport report={detail.data} />}</QueryState>}
      <Panel>
        <SectionHeading title="受控离线重放" description="从本地登记档案读取原始内容，在临时数据库内运行，全程零网络请求。"
          action={<Badge tone="accent">隔离运行</Badge>} />
        <QueryState {...archive}>
          {replayable.length ? <form onSubmit={event => { event.preventDefault(); void runReplay() }} className="dc-replay-form">
            <fieldset disabled={running}>
              <legend>选择可重放档案</legend>
              {replayable.map(item => <label key={item.fetch_id} className="dc-check-row">
                <input type="checkbox" checked={fetchIds.includes(item.fetch_id)}
                  disabled={fetchIds.length >= 8 && !fetchIds.includes(item.fetch_id)}
                  onChange={event => setFetchIds(current => event.target.checked ? [...current, item.fetch_id] : current.filter(id => id !== item.fetch_id))} />
                <span><strong>{item.target_key}</strong><small>{item.filename}</small></span>
              </label>)}
            </fieldset>
            <p className="dc-notice">已选择 {fetchIds.length} / 8 个档案。</p>
            <button type="submit" className="dc-button dc-primary" disabled={!fetchIds.length || running}>
              <Play size={15} aria-hidden />{running ? '正在重放…' : '运行离线重放'}
            </button>
          </form> : <Empty title="暂无可重放的已登记档案" />}
        </QueryState>
        {replayError && <ErrorState message={replayError} />}
      </Panel>
      {replay && <RunReport report={replay} />}
    </div>
  </>
}
