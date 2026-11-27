import { useState } from 'react'
import { Download } from 'lucide-react'
import { Badge, Empty, ErrorState, Loading, PageHeading, Panel } from '../components/ui'
import { useApi } from '../hooks/useApi'
import './DailyReport.css'

type WorkflowRun = {
  task_id: string
  created_at: string
  mode: 'mock' | 'live'
  status: string
  draft_title: string | null
  review_status: string | null
}

type DailyData = {
  day: string
  timezone: string
  current_version: string
  git: {
    head: string
    commits: { sha: string; committed_at: string; subject: string }[]
    truncated: boolean
  } | null
  test_snapshot: {
    scope: string
    verified_at: string
    suites: { label: string; passed: number; failed: number; skipped: number }[]
  } | null
  workflow_runs: WorkflowRun[] | null
  ignored_workflow_exports: number | null
}

const statuses: Record<string, string> = {
  draft_ready: '草稿已生成',
  pending_review: '待人工审核',
  needs_manual_review: '需人工处理',
  aborted: '已中止',
  budget_exhausted: '预算已耗尽',
  failed: '失败',
  passed: '规则检查通过',
  needs_revision: '需修订',
}

const noteFields = [
  ['completed', '当日完成工作'],
  ['outcomes', '可展示业务成果'],
  ['issues', '当前问题'],
  ['next', '下一步计划'],
] as const
type Notes = Partial<Record<(typeof noteFields)[number][0], string>>

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
  }).format(new Date(value))
}

function runLabel(run: WorkflowRun) {
  const review = run.review_status ? statuses[run.review_status] ?? run.review_status : '无审核记录'
  return `${run.mode === 'mock' ? 'Mock' : 'Live'} · ${statuses[run.status] ?? run.status} · ${review}`
}

const costLines = [
  '研发模型 · Token 待统计，API 费用待对账。',
  'PowerCopy 运行 · API 调用及输入、输出、总 Token 待统计，预计费用待统计，实际费用待对账。',
  '单任务及每日预算 · 待接入预算服务。',
]

function exportReport(report: DailyData, notes: Notes) {
  const content = [
    `# PowerCopy 日报 · ${report.day}`,
    '统计时区 Asia/Shanghai（UTC+8）',
    ...noteFields.flatMap(([key, label]) => [`## ${label}`, notes[key]?.trim() || '未填写。']),
    '## 已归档文案及工作流审核',
    ...(report.workflow_runs?.length
      ? report.workflow_runs.map(run => `- ${run.draft_title || run.task_id} · ${runLabel(run)} · ${formatTime(run.created_at)} · ${run.task_id}`)
      : [report.workflow_runs ? '当日未找到工作流导出。' : '工作流归档暂不可用。']),
    ...(report.ignored_workflow_exports ? [`归档目录中有 ${report.ignored_workflow_exports} 份文件无法读取或校验。`] : []),
    '人工审批待接入。',
    '## Git 提交及版本',
    `当前工作树版本 ${report.current_version} · HEAD ${report.git?.head ?? '未读取'}`,
    ...(report.git?.commits.length
      ? report.git.commits.map(commit => `- ${commit.sha} · ${commit.subject} · ${formatTime(commit.committed_at)}`)
      : [report.git ? '当前分支历史未找到当日提交。' : 'Git 记录暂不可用。']),
    ...(report.git?.truncated ? ['仅列出 100 条提交。'] : []),
    '## 测试结果',
    ...(report.test_snapshot
      ? [report.test_snapshot.scope, ...report.test_snapshot.suites.map(suite =>
        `- ${suite.label} · 通过 ${suite.passed} · 失败 ${suite.failed} · 跳过 ${suite.skipped}`)]
      : ['未找到当日测试快照。']),
    '## Token 与 API 费用',
    ...costLines,
  ].join('\n\n')
  const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `powercopy-daily-${report.day}.md`
  link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function DailyReport() {
  const [day, setDay] = useState('')
  const [notesByDay, setNotesByDay] = useState<Record<string, Notes>>({})
  const report = useApi<DailyData>(day ? `/reports/daily?day=${encodeURIComponent(day)}` : '/reports/daily')
  const data = report.data
  const notes = data ? notesByDay[data.day] ?? {} : {}

  function updateNote(key: keyof Notes, value: string) {
    if (!data) return
    setNotesByDay(previous => ({ ...previous, [data.day]: { ...previous[data.day], [key]: value } }))
  }

  return <>
    <PageHeading title="每日工作与产出" description="查看本地工作流、研发记录与费用状态，补充工作内容后导出日报。">
      <button className="button button-primary" type="button" disabled={!data}
        onClick={() => data && exportReport(data, notes)}>
        <Download size={16} aria-hidden="true" /> 导出日报
      </button>
    </PageHeading>
    <div className="daily-filter">
      <label htmlFor="daily-day">报告日期</label>
      <input id="daily-day" className="field" type="date" value={day || data?.day || ''}
        onChange={event => setDay(event.target.value)} />
      <span className="muted">北京时间 UTC+8</span>
    </div>
    {report.loading && <Panel><Loading /></Panel>}
    {report.error && <Panel><ErrorState message={report.error} retry={report.reload} /></Panel>}
    {data && <div className="daily-grid">
      <Panel className="daily-panel">
        <h2>研发事项</h2>
        <p className="muted">补充内容保留在当前页面，并随日报导出。</p>
        {noteFields.map(([key, label]) =>
          <label className="daily-note" key={key}>{label}
            <textarea className="field" rows={3} value={notes[key] ?? ''} maxLength={4000}
              onChange={event => updateNote(key, event.target.value)} />
          </label>,
        )}
      </Panel>
      <Panel className="daily-panel">
        <h2>已归档文案及工作流审核</h2>
        <p className="muted">数据来自本机保存的工作流导出。人工审批待接入。</p>
        {data.workflow_runs?.length ? <ul className="daily-list">
          {data.workflow_runs.map(run => <li key={run.task_id}>
            <strong>{run.draft_title || '无草稿'}</strong>
            <span>{runLabel(run)}</span>
            <small>{formatTime(run.created_at)} · <code>{run.task_id}</code></small>
          </li>)}
        </ul> : <Empty title={data.workflow_runs ? '当日未找到工作流导出' : '工作流归档暂不可用'} />}
        {!!data.ignored_workflow_exports && <p className="muted">归档目录中有 {data.ignored_workflow_exports} 份文件无法读取或校验。</p>}
      </Panel>
      <Panel className="daily-panel">
        <h2>Git 提交及版本</h2>
        <p className="muted">当前工作树 <code>{data.current_version}</code> · HEAD <code>{data.git?.head.slice(0, 12) ?? '未读取'}</code></p>
        {data.git?.commits.length ? <ul className="daily-list">
          {data.git.commits.map(commit => <li key={commit.sha}>
            <strong>{commit.subject}</strong>
            <small><code>{commit.sha.slice(0, 12)}</code> · {formatTime(commit.committed_at)}</small>
          </li>)}
        </ul> : <Empty title={data.git ? '当前分支历史未找到当日提交' : 'Git 记录暂不可用'} />}
        {data.git?.truncated && <p className="muted">仅列出 100 条提交。</p>}
      </Panel>
      <Panel className="daily-panel">
        <h2>测试结果</h2>
        {data.test_snapshot ? <>
          <p className="muted">{data.test_snapshot.scope} · {formatTime(data.test_snapshot.verified_at)}</p>
          <ul className="daily-list">{data.test_snapshot.suites.map(suite => <li key={suite.label}>
            <strong>{suite.label}</strong><span>通过 {suite.passed} · 失败 {suite.failed} · 跳过 {suite.skipped}</span>
          </li>)}</ul>
        </> : <Empty title="未找到当日测试快照" />}
        <h3>Token 与 API 费用</h3>
        <ul className="daily-list">{costLines.map(line => <li key={line}>{line}</li>)}</ul>
        <Badge tone="warning">成本核算服务待接入</Badge>
      </Panel>
    </div>}
  </>
}
