import { useState } from 'react'
import { GitCompareArrows } from 'lucide-react'
import { Badge, Empty, PageHeading, Panel } from '../../components/ui'
import { useApi } from '../../hooks/useApi'
import { DataTable, Hash, QueryState, RelatedLink, routeParam, SectionHeading, timeLabel, withQuery } from './common'
import type { Collection, Policies, Policy, PolicyDiff } from './models'
import { PolicyDetailPanel } from './PoliciesPage'

function VersionCompare({ policyPath, items }: { policyPath: string; items: Policy[] }) {
  const [from, setFrom] = useState(items.at(-1)?.version ?? 1)
  const [to, setTo] = useState(items[0]?.version ?? 1)
  const [comparison, setComparison] = useState<{ from_version: number; to_version: number }>()
  const query = useApi<PolicyDiff>(comparison ? withQuery(policyPath + '/diff', comparison) : null)
  return <Panel>
    <SectionHeading title="版本差异" description="对比正文内容与关键字段。" />
    <form className="dc-filter-bar" onSubmit={event => { event.preventDefault(); setComparison({ from_version: from, to_version: to }) }}>
      <label>起始版本<select value={from} onChange={event => setFrom(Number(event.target.value))}>
        {items.map(item => <option key={item.version} value={item.version}>v{item.version}</option>)}
      </select></label>
      <label>目标版本<select value={to} onChange={event => setTo(Number(event.target.value))}>
        {items.map(item => <option key={item.version} value={item.version}>v{item.version}</option>)}
      </select></label>
      <button className="dc-button dc-primary" type="submit" disabled={from === to || query.loading}><GitCompareArrows size={15} aria-hidden />开始对比</button>
    </form>
    {comparison && <QueryState {...query}>{query.data && <div className="dc-diff">
      <p className="dc-muted">v{query.data.from_version} → v{query.data.to_version}</p>
      {query.data.metadata.map(item => <div className="dc-diff-meta" key={item.field}><strong>{item.field}</strong><span>{item.before || '—'}</span><span aria-hidden>→</span><span>{item.after || '—'}</span></div>)}
      {!!query.data.changes.length && <pre>{query.data.changes.map((change, index) => {
        if (change.kind === 'insert') return <ins key={index}>{change.text}</ins>
        if (change.kind === 'delete') return <del key={index}>{change.text}</del>
        return <span key={index}>{change.text}</span>
      })}</pre>}
    </div>}</QueryState>}
  </Panel>
}

function VersionHistory({ policyId }: { policyId: string }) {
  const path = '/policies/' + encodeURIComponent(policyId)
  const versions = useApi<Collection<Policy>>(path + '/versions')
  const [detail, setDetail] = useState<number>()
  const items = versions.data?.items ?? []
  return <div className="dc-stack">
    <Panel>
      <SectionHeading title="版本时间线" description={items[0]?.title} action={<Badge tone="accent">{versions.data?.items.length ?? '—'} 个版本</Badge>} />
      <QueryState {...versions}>
        {items.length ? <DataTable label="政策版本时间线" headers={['版本', '发布日期', '正文长度', '内容哈希', '最近观测', '版本详情']}>
          {items.map(item => <tr key={item.version} data-selected={detail === item.version}>
            <th scope="row"><Badge tone={item.is_latest_observed ? 'success' : 'neutral'}>v{item.version}</Badge></th>
            <td>{item.published_on || '—'}</td>
            <td>{item.body_length.toLocaleString()} 字符</td>
            <td><Hash value={item.content_sha256} /></td>
            <td>{timeLabel(item.last_observed_at)}</td>
            <td><button type="button" className="dc-text-button" onClick={() => setDetail(item.version)} aria-pressed={detail === item.version}>查看详情</button></td>
          </tr>)}
        </DataTable> : <Empty title="暂无版本记录" />}
      </QueryState>
    </Panel>
    {detail && <PolicyDetailPanel key={detail} policyId={policyId} version={detail} onClose={() => setDetail(undefined)} />}
    {items.length > 1 && <VersionCompare policyPath={path} items={items} />}
    {items.length === 1 && <Panel><SectionHeading title="版本差异" description="当前政策只有 1 个已观测版本。有两个以上版本时可进行对比。" /></Panel>}
  </div>
}

export default function VersionsPage() {
  const [policyId, setPolicyId] = useState(routeParam('policy_id'))
  const [pendingId, setPendingId] = useState(routeParam('policy_id'))
  const policies = useApi<Policies>('/policies?limit=50')
  return <>
    <PageHeading eyebrow="Version History" title="版本历史" description="查看指定政策的内容版本与观测时间，核对历史正文和内容差异。">
      {policyId && <RelatedLink view="policies" query={{ policy_id: policyId }}>返回政策详情</RelatedLink>}
    </PageHeading>
    <div className="dc-stack">
      <Panel>
        <form className="dc-filter-bar" onSubmit={event => { event.preventDefault(); setPolicyId(pendingId) }}>
          <label className="dc-wide">选择政策
            <select value={pendingId} onChange={event => setPendingId(event.target.value)} required>
              <option value="">选择要查看版本的政策</option>
              {policyId && !policies.data?.items.some(item => item.policy_id === policyId) && <option value={policyId}>{policyId}</option>}
              {policies.data?.items.map(item => <option key={item.policy_id} value={item.policy_id}>{item.title}</option>)}
            </select>
          </label>
          <button type="submit" className="dc-button dc-primary">载入版本</button>
          <RelatedLink view="policies">浏览政策库</RelatedLink>
        </form>
      </Panel>
      {policyId ? <VersionHistory key={policyId} policyId={policyId} /> :
        <Empty title="尚未选择政策" description="可从列表选择一条政策，或在政策库中打开版本历史。" />}
    </div>
  </>
}
