import { useMemo, useState, type FormEvent } from 'react'
import {
  AlertTriangle, ArrowDownToLine, ArrowUpRight, Clipboard, FileText,
  LoaderCircle, RotateCcw, ShieldAlert, Sparkles,
} from 'lucide-react'
import { officialLink, post } from '../api/client'
import { Badge, Empty, ErrorState, Loading, PageHeading, Panel } from '../components/ui'
import { useApi } from '../hooks/useApi'
import type {
  ContentMode, ContentPlatform, ContentRequest, ContentRun, ContentStatus,
  EvidenceItem, PolicyValidity, ResearchClaim,
} from '../types/content'
import './ContentStudio.css'

const platformLabels: Record<ContentPlatform, string> = {
  wechat_mp: '微信公众号', website: '企业网站', linkedin: 'LinkedIn', general: '通用平台',
}

const validityLabels: Record<PolicyValidity, string> = {
  latest_observed: '本地库最新观测版本，不代表现行有效',
  superseded_in_corpus: '本地资料中存在更新版本',
  unknown: '政策时效待核实',
}

const statusLabels: Record<string, string> = {
  pending_review: '待人工审核',
  failed: '生成失败',
  aborted: '证据不足',
  candidate: '候选事实',
  verified: '引文原文已核验',
  rejected: '未通过引文核验',
  success: '生成完成',
  empty: '无结果',
  partial: '部分完成',
  completed: '完成',
  draft_ready: '文案草稿已生成',
  needs_manual_review: '需人工审核',
  budget_exhausted: '任务预算已耗尽',
  passed: '规则检查通过',
  needs_revision: '需修订',
  machine_supported: '机器判断有支持',
  revised: '已修订',
  unchanged: '修订未改动',
  skipped: '未执行',
}

function StatusBadge({ value }: { value: string }) {
  const tone = value === 'verified' || value === 'success' || value === 'completed' ? 'success' :
    value === 'failed' || value === 'rejected' ? 'error' :
      value === 'pending_review' || value === 'candidate' || value === 'aborted' || value === 'budget_exhausted' ? 'warning' : 'neutral'
  return <Badge tone={tone}>{statusLabels[value] ?? value}</Badge>
}

function timestamp(value: string) {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '时间未记录'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
  }).format(date)
}

function countCharacters(value: string) {
  return [...value].filter(character => !/\s/u.test(character)).length
}

function EvidenceCard({ item, selected }: { item: EvidenceItem; selected: boolean }) {
  const policyLink = `#/data/policies?policy_id=${encodeURIComponent(item.policy_id)}&version=${item.version}`
  const sourceUrl = officialLink(item.source_url)
  return <details id={`content-evidence-${item.evidence_id}`} className="content-evidence" open={selected || undefined}>
    <summary>
      <span className="content-evidence-title"><strong>{item.title}</strong><StatusBadge value={item.status} /></span>
      <span className="content-evidence-meta">v{item.version} · [{item.start}, {item.end}){item.page != null ? ` · PDF 第 ${item.page} 页` : ''}</span>
    </summary>
    <blockquote>{item.exact_quote}</blockquote>
    <dl className="content-evidence-details">
      <div><dt>Evidence ID</dt><dd><code>{item.evidence_id}</code></dd></div>
      <div><dt>Document ID</dt><dd><code>{item.document_id}</code></dd></div>
      <div><dt>正文哈希</dt><dd><code>{item.content_sha256}</code></dd></div>
    </dl>
    <div className="content-actions">
      <a className="button button-quiet" href={policyLink}><FileText size={14} aria-hidden="true" />查看对应政策版本</a>
      {sourceUrl && <a className="button button-quiet" href={sourceUrl} target="_blank" rel="noopener noreferrer">
        官方来源 <ArrowUpRight size={13} aria-hidden="true" />
      </a>}
    </div>
  </details>
}

function ClaimCard({ claim, evidenceById }: { claim: ResearchClaim; evidenceById: Map<string, EvidenceItem> }) {
  const citations = claim.evidence_ids.map(id => evidenceById.get(id)).filter((item): item is EvidenceItem => Boolean(item))
  return <li className="content-claim">
    <div className="content-claim-head"><StatusBadge value={claim.status} /><span>{claim.claim_id}</span></div>
    <p>{claim.statement}</p>
    {claim.support_note && <small>{claim.support_note}</small>}
    <ul className="content-claim-links">{citations.map(item => <li key={item.evidence_id}>
      <a href={`#/data/policies?policy_id=${encodeURIComponent(item.policy_id)}&version=${item.version}`}>
        {item.title} · [{item.start}, {item.end})
      </a>
    </li>)}</ul>
  </li>
}

export default function ContentStudio() {
  const [mode, setMode] = useState<ContentMode>('mock')
  const [authorized, setAuthorized] = useState(false)
  const [run, setRun] = useState<ContentRun | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [copyNotice, setCopyNotice] = useState('')
  const [editedTitle, setEditedTitle] = useState<string>()
  const [editedBody, setEditedBody] = useState<string>()
  const [selectedEvidence, setSelectedEvidence] = useState<string>()
  const status = useApi<ContentStatus>('/content/status')

  const draft = run?.content_draft
  const editableDraft = draft?.status === 'pending_review' ? draft : null
  const research = run?.evidence_package
  const title = draft ? editedTitle ?? draft.title : ''
  const body = draft ? editedBody ?? draft.body : ''
  const edited = Boolean(draft && (title !== draft.title || body !== draft.body))
  const bodyCount = countCharacters(body)
  const evidenceById = useMemo(
    () => new Map((research?.evidence_items ?? []).map(item => [item.evidence_id, item])),
    [research],
  )

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || !status.data?.mock_available || (mode === 'live' && (!authorized || !status.data.live_available))) return
    const form = new FormData(event.currentTarget)
    const topic = String(form.get('topic')).trim()
    if (!topic) {
      setError('请输入文案主题。')
      return
    }
    const request: ContentRequest = {
      topic,
      target_audience: String(form.get('target_audience')).trim(),
      region: String(form.get('region')),
      platform: String(form.get('platform')) as ContentPlatform,
      target_chars: Number(form.get('target_chars')),
      style: String(form.get('style')).trim(),
      purpose: String(form.get('purpose')).trim(),
      mode,
      confirm_live: authorized,
    }
    setPending(true)
    setError('')
    setCopyNotice('')
    setRun(null)
    setAuthorized(false)
    setSelectedEvidence(undefined)
    setEditedTitle(undefined)
    setEditedBody(undefined)
    try {
      setRun(await post<ContentRun>('/content/runs', request))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '创作请求失败。')
    } finally {
      setPending(false)
    }
  }

  async function copyDraft() {
    if (!editableDraft) return
    try {
      await navigator.clipboard.writeText(`${title}\n\n${body}`)
      setCopyNotice('草稿已复制到剪贴板。')
    } catch {
      setCopyNotice('无法访问剪贴板，请手动选择并复制草稿。')
    }
  }

  function exportDraft() {
    if (!editableDraft) return
    const file = new Blob([`${title}\n\n${body}`], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(file)
    const link = document.createElement('a')
    link.href = url
    link.download = `${editableDraft.draft_id}.txt`
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  const evidenceItems = research?.evidence_items ?? []
  const researchStatistics = run?.research_statistics
  const draftStatistics = run?.draft_statistics

  return <>
    <PageHeading eyebrow="CONTENT WORKFLOW" title="Content Studio"
      description="围绕政策原文创建待人工审核的可追溯文案草稿。" />
    <div className="content-workspace">
      <Panel className="content-brief">
        <header className="content-panel-head"><span className="panel-icon"><Sparkles size={17} aria-hidden="true" /></span>
          <div><h2 id="content-brief-heading">创作需求</h2><p>所有输入由服务端重新校验。</p></div></header>
        {status.loading && <Loading />}
        {status.error && <ErrorState message={`创作服务不可用。 ${status.error}`} retry={status.reload} />}
        {status.data && <div className="content-service-state">
          <span>内容工作流</span><Badge tone={status.data.mock_available ? 'success' : 'warning'}>
            {status.data.mock_available ? '就绪' : '暂不可用'}
          </Badge>
        </div>}
        {status.data && !status.data.mock_available && <p className="content-mode-note">
          {status.data.workflow_available ? '政策库或索引未就绪。' : '统一内容工作流尚未接入当前后端。'}
        </p>}
        {<form className="content-brief-form" onSubmit={submit}
          aria-labelledby="content-brief-heading">
          <div className="content-field">
            <label htmlFor="content-topic">文案主题 <span aria-hidden="true">*</span></label>
            <textarea id="content-topic" name="topic" rows={3} className="field" maxLength={200} required
              placeholder="描述需要研究的电力政策问题" />
          </div>
          <div className="content-field">
            <label htmlFor="content-audience">目标客户</label>
            <input id="content-audience" name="target_audience" className="field" maxLength={100} placeholder="例如：工商业用电企业" />
          </div>
          <div className="content-field">
            <label htmlFor="content-region">目标地区</label>
            <select id="content-region" name="region" className="field" defaultValue="">
              <option value="">未指定</option>
              <option>江西</option><option>安徽</option><option>广东</option><option>山西</option>
            </select>
          </div>
          <div className="content-field-pair">
            <div className="content-field"><label htmlFor="content-platform">发布平台</label>
              <select id="content-platform" name="platform" className="field" defaultValue="wechat_mp">
                {Object.entries(platformLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </div>
            <div className="content-field"><label htmlFor="content-length">目标字数</label>
              <input id="content-length" name="target_chars" type="number" className="field"
                min={100} max={5000} step={1} defaultValue={800} required />
            </div>
          </div>
          <div className="content-field"><label htmlFor="content-style">表达风格</label>
            <input id="content-style" name="style" className="field" maxLength={200} defaultValue="专业、通俗、可信" required />
          </div>
          <div className="content-field"><label htmlFor="content-purpose">创作目的</label>
            <textarea id="content-purpose" name="purpose" className="field" rows={2} maxLength={500}
              placeholder="说明读者需要了解或行动的事项" />
          </div>
          <fieldset className="content-mode-fieldset">
            <legend>运行模式</legend>
            <div className="content-mode-choice">
              <label><input type="radio" name="content-mode" checked={mode === 'mock'}
                onChange={() => { setMode('mock'); setAuthorized(false) }} />本地 Mock</label>
              <label><input type="radio" name="content-mode" value="live" disabled={!status.data?.live_available}
                checked={mode === 'live'} onChange={() => setMode('live')} />
                真实模型{status.data?.live_available ? '' : '（服务端未开启）'}
              </label>
            </div>
          </fieldset>
          {mode === 'live' && <label className="content-live-confirm">
            <input type="checkbox" checked={authorized} onChange={event => setAuthorized(event.target.checked)} />
            授权本次请求调用真实模型
          </label>}
          <button className="button button-primary" type="submit"
            disabled={pending || !status.data?.mock_available || (mode === 'live' && (!authorized || !status.data?.live_available))}>
            {pending ? <LoaderCircle size={15} className="spinner" aria-hidden="true" /> : <Sparkles size={15} aria-hidden="true" />}
            {pending ? '正在研究并生成草稿…' : '开始创作'}
          </button>
          <p className="content-mode-note">{mode === 'mock'
            ? '本地 Mock 使用现有业务流程，不调用外部模型。'
            : '服务端仍需显式启用 Live 并校验本次授权。'}</p>
        </form>}
      </Panel>

      <div className="content-center">
        {error && <Panel><ErrorState message={error} /></Panel>}
        <Panel className="content-research">
          <header className="content-panel-head"><span className="panel-icon"><FileText size={17} aria-hidden="true" /></span>
            <div><h2>政策研究</h2><p>研究问题、候选结论与未解决事项。</p></div></header>
          {research ? <>
            <div className="content-result-meta"><StatusBadge value={research.research_status} />
              <Badge tone="accent">{research.research_id}</Badge><span>{timestamp(research.created_at)}</span></div>
            <p className="content-result-reason">{research.status_reason}</p>
            {research.policy_validity_note && <div className="content-validity" role="note">
              <ShieldAlert size={15} aria-hidden="true" /><span>{research.policy_validity_note}</span>
            </div>}
            {research.research_questions.length > 0 && <ol className="content-question-list">
              {research.research_questions.map(question => <li key={question.question_id}>
                <span>{question.text}</span><Badge tone={question.answered ? 'accent' : 'warning'}>{question.answered ? '已有候选依据' : '未解决'}</Badge>
                {question.answer && <p>{question.answer}</p>}
              </li>)}
            </ol>}
            {[...research.unanswered_questions, ...research.uncertainties].length > 0 &&
              <div className="content-open-questions"><strong>未解决问题与不确定性</strong>
                <ul>{[...research.unanswered_questions, ...research.uncertainties].map((question, index) =>
                  <li key={`${index}-${question}`}>{question}</li>)}</ul>
              </div>}
            <div className="content-result-meta">
              <span>{research.claims.length} 条研究主张</span>
              {researchStatistics && <>
                <span>{researchStatistics.claims_text_verified} 条主张的引文通过原文核验</span>
                <span>{researchStatistics.claims_pending_semantic_review} 条主张语义待审核</span>
                <span>{researchStatistics.human_confirmation_available ? `${researchStatistics.claims_human_confirmed} 条人工确认` : '人工确认通道未接入'}</span>
              </>}
            </div>
          </> : <Empty title={pending ? '正在检索政策原文…' : '等待研究结果'}
            description="提交创作需求后，研究问题和未解决事项会显示在这里。" />}
        </Panel>

        <Panel className="content-draft-panel">
          <header className="content-panel-head"><span className="panel-icon"><FileText size={17} aria-hidden="true" /></span>
            <div><h2>文案草稿</h2><p>草稿不会自动发布。</p></div>
            {editableDraft && <div className="content-draft-actions">
              <span role="status" className="content-copy-notice">{copyNotice}</span>
              <button type="button" className="icon-button" title="复制草稿" aria-label="复制草稿" onClick={() => void copyDraft()}>
                <Clipboard size={15} aria-hidden="true" /></button>
              <button type="button" className="icon-button" title="导出草稿" aria-label="导出草稿" onClick={exportDraft}>
                <ArrowDownToLine size={15} aria-hidden="true" /></button>
              <button type="button" className="icon-button" title="恢复生成版本" aria-label="恢复生成版本" disabled={!edited}
                onClick={() => { setEditedTitle(undefined); setEditedBody(undefined) }}>
                <RotateCcw size={15} aria-hidden="true" /></button>
            </div>}
          </header>
          {pending && <p role="status" className="content-inline-pending"><LoaderCircle size={14} className="spinner" />正在等待完整业务流程响应…</p>}
          {run && <div className="content-draft-state"><StatusBadge value={run.status} /><span>{run.status_reason}</span></div>}
          {editableDraft ? <>
            <div className="content-draft-state">
              <span>模型 {editableDraft.model_name} · {editableDraft.prompt_version} · {editableDraft.generation_mode === 'mock' ? '本地 Mock' : '真实模型'}</span>
            </div>
            {edited && <p className="content-warning" role="status">
              <AlertTriangle size={15} aria-hidden="true" />草稿已修改，旧审核结果和证据映射可能失效，当前文本待重新审核。
            </p>}
            <div className="content-field"><label htmlFor="draft-title">文案标题</label>
              <input id="draft-title" className="field" value={title} maxLength={100}
                onChange={event => setEditedTitle(event.target.value)} />
            </div>
            <div className="content-field"><label htmlFor="draft-body">文案正文</label>
              <textarea id="draft-body" className="field content-body-editor" value={body} maxLength={20_000}
                onChange={event => setEditedBody(event.target.value)} />
            </div>
            <div className="content-draft-meta">
              <span>{bodyCount.toLocaleString('zh-CN')} / {editableDraft.target_chars.toLocaleString('zh-CN')} 字</span>
              <span>{platformLabels[editableDraft.platform]}</span>
              <span>任务 ID：<code>{run?.task_id ?? '—'}</code></span>
            </div>
            <div className="content-validity" role="note"><ShieldAlert size={15} aria-hidden="true" />
              <span>{editableDraft.policy_validity_note || validityLabels[editableDraft.policy_validity_status]} {editableDraft.region_applicability || '地区适用性待核实。'}</span>
            </div>
            {draftStatistics && <div className="content-result-meta">
              <span>{draftStatistics.key_points_bound_to_evidence} / {draftStatistics.key_points_total} 个要点绑定证据</span>
              <span>{draftStatistics.key_points_pending_semantic_review} 个要点语义待审核</span>
              <span>{draftStatistics.human_confirmation_available ? '人工确认通道可用' : '人工确认通道未实现'}</span>
            </div>}
            {editableDraft.review_notes.length > 0 && <div className="content-open-questions">
              <strong>人工审核待办</strong><ul>{editableDraft.review_notes.map((note, index) => <li key={`${index}-${note}`}>{note}</li>)}</ul>
            </div>}
            {editableDraft.key_points.length > 0 && <div className="content-key-points"><strong>{edited ? '生成版本的事实要点与证据' : '事实要点与证据关联'}</strong>
              <ul>{editableDraft.key_points.map(point => <li key={point.point_id}>
                <p>{point.text}</p><StatusBadge value={point.semantic_status} />
                <div className="content-actions">{point.evidence_ids.map(id => evidenceById.has(id) && <button
                  key={id} type="button" className="button button-quiet" onClick={() => {
                    setSelectedEvidence(id)
                    const evidence = document.getElementById(`content-evidence-${id}`)
                    if (evidence instanceof HTMLDetailsElement) {
                      evidence.open = true
                      evidence.scrollIntoView({ block: 'nearest' })
                      evidence.querySelector('summary')?.focus({ preventScroll: true })
                    }
                  }}>查看引用 {id.slice(0, 6)}</button>)}</div>
                {point.claim_ids.map(claimId => {
                  const claim = research?.claims.find(value => value.claim_id === claimId)
                  return claim ? <details key={claimId}>
                    <summary>查看对应研究主张</summary><ul className="content-claim-list">
                      <ClaimCard claim={claim} evidenceById={evidenceById} />
                    </ul>
                  </details> : null
                })}
                {!point.claim_ids.length && <span className="content-warning-text">未关联研究主张</span>}
              </li>)}</ul>
            </div>}
          </> : <Empty title={pending ? '等待工作流响应…' : (run?.status === 'aborted' ? '证据不足，未生成文案' : '暂无文案草稿')}
            description={draft?.status_reason || (!run ? '完成政策研究后，生成草稿会显示在这里。' : undefined)} />}
        </Panel>
      </div>

      <Panel className="content-evidence-panel">
        <header className="content-panel-head"><span className="panel-icon"><FileText size={17} aria-hidden="true" /></span>
          <div><h2>证据与审核</h2><p>每条引用都可追溯至政策版本和正文字符区间。</p></div></header>
        {research && <>
          <div className="content-validity" role="note"><ShieldAlert size={15} aria-hidden="true" />
            <span>{research.policy_validity_note || validityLabels[research.policy_validity_status]}</span>
          </div>
          {research.region && <p className="content-region-note">请求地区：{research.region}。政策适用范围需人工核实。</p>}
          <div className="content-result-meta">
            {evidenceItems.length > 0 && <StatusBadge value={evidenceItems.some(item => item.status === 'candidate') ? 'candidate' : 'verified'} />}
            <span>{evidenceItems.filter(item => item.status === 'verified').length} 条引文与原文位置核验一致</span>
            <span>语义支持需人工确认</span>
          </div>
          {evidenceItems.length > 0 ? <div className="content-evidence-list">
            {evidenceItems.map(item => <EvidenceCard item={item} key={item.evidence_id} selected={selectedEvidence === item.evidence_id} />)}
          </div> : <Empty title="本次研究未返回证据" />}
          {research.verification_issues.length > 0 && <div className="content-open-questions">
            <strong>引文核验问题</strong><ul>{research.verification_issues.map((issue, index) =>
              <li key={`${issue.code}-${index}`}>{issue.code}{issue.detail && ` · ${issue.detail}`}</li>,
            )}</ul>
          </div>}
        </>}
        {!research && <Empty title="等待可追溯证据" description="完成研究后，已核验引文及其原文位置会显示在这里。" />}
        {run && <ReviewSummary run={run} edited={edited} />}
        {editableDraft && <div className="content-generation-trace">
          <strong>生成记录</strong>
          <dl><div><dt>创建时间</dt><dd>{timestamp(editableDraft.created_at)}</dd></div>
            <div><dt>模型</dt><dd>{editableDraft.model_name}</dd></div>
            <div><dt>已知 Token</dt><dd>{run?.budget?.known_total_tokens ?? '未记录'}</dd></div>
            <div><dt>已用 / 最大模型尝试</dt><dd>{run?.budget?.used_api_attempts ?? '—'} / {run?.budget?.max_api_attempts ?? '—'}</dd></div>
            {run?.budget?.usage_incomplete && <div><dt>Token 统计</dt><dd>部分模型用量未知</dd></div>}</dl>
        </div>}
      </Panel>
    </div>
  </>
}

function ReviewSummary({ run, edited }: { run: ContentRun; edited: boolean }) {
  const report = run.review_report
  if (edited || !report) return <div className="content-validity" role="note">
    <ShieldAlert size={15} aria-hidden="true" /><span>{edited ? '编辑后的草稿未审核。'
      : run.content_draft?.status === 'pending_review' ? '当前草稿未审核，语义支持性待人工确认。' : '本次尚无草稿审核记录。'}</span>
  </div>
  const labels: Record<string, string> = {
    citation: '引用完整性', fact_support: '事实支持性', compliance: '宣传合规', applicability: '政策适用性',
  }
  return <section className="content-generation-trace" aria-label="审核记录">
    <div className="content-result-meta"><strong>审核记录</strong><StatusBadge value={report.status} /></div>
    <p className="content-validity" role="note"><ShieldAlert size={15} aria-hidden="true" />
      <span>{report.fact_support_llm_used ? '事实支持性为机器审核，待专家核实。' : '事实支持性待人工确认。'}</span>
    </p>
    <dl>{report.dimension_results.map(dimension => <div key={dimension.dimension}>
      <dt>{labels[dimension.dimension] ?? dimension.dimension}</dt><dd><StatusBadge value={dimension.verdict} />
        {dimension.detail && <p>{dimension.detail}</p>}</dd>
    </div>)}</dl>
    {report.issues.length > 0 && <ul className="content-review-list">{report.issues.map(issue =>
      <li key={issue.issue_id}><strong>{issue.code}</strong><p>{issue.message}</p></li>)}</ul>}
    {run.revision_records.map(revision => <details key={revision.revision_id} className="content-evidence">
      <summary>自动修订 · {statusLabels[revision.status] ?? revision.status}</summary>
      {revision.changed && <>
        <h3>修订前</h3><blockquote>{revision.before_title}{'\n'}{revision.before_body}</blockquote>
        <h3>修订后</h3><blockquote>{revision.after_title}{'\n'}{revision.after_body}</blockquote>
      </>}
    </details>)}
  </section>
}
