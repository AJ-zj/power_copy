import { useState } from 'react'
import { ChevronLeft, ChevronRight, Search, X } from 'lucide-react'
import { Badge, Empty, PageHeading, Panel } from '../../components/ui'
import { useApi } from '../../hooks/useApi'
import { DataTable, Hash, OfficialLink, PageControls, QueryState, RelatedLink, routeParam, SectionHeading, timeLabel, withQuery } from './common'
import type { Policy, Policies, PolicyDetail } from './models'

const limit = 20

export function PolicyDetailPanel({ policyId, version, onClose }: { policyId: string; version?: number; onClose: () => void }) {
  const [offset, setOffset] = useState(0)
  const query = useApi<PolicyDetail>(withQuery('/policies/' + encodeURIComponent(policyId), { version, offset }))
  return <Panel className="dc-detail-panel">
    <div className="dc-detail-close"><button className="dc-icon-button" onClick={onClose} aria-label="关闭政策详情"><X size={17} aria-hidden /></button></div>
    <QueryState {...query}>{query.data && <article>
      <div className="dc-header-tags"><Badge tone="accent">版本 v{query.data.version}</Badge>
        {query.data.is_latest_observed && <Badge tone="success">最近观测</Badge>}</div>
      <h2>{query.data.title}</h2>
      <dl className="dc-meta-grid">
        <div><dt>发布机构</dt><dd>{query.data.issuer || '未标注'}</dd></div>
        <div><dt>地区</dt><dd>{query.data.region || '未标注'}</dd></div>
        <div><dt>发布日期</dt><dd>{query.data.published_on || '未标注'}</dd></div>
        <div><dt>最近获取</dt><dd>{timeLabel(query.data.last_observed_at)}</dd></div>
        <div><dt>正文长度</dt><dd>{query.data.body_length.toLocaleString()} 字符</dd></div>
        <div><dt>观测次数</dt><dd>{query.data.observation_count}</dd></div>
        <div><dt>内容哈希</dt><dd><Hash value={query.data.content_sha256} /></dd></div>
      </dl>
      <div className="dc-detail-actions">
        {!version && <RelatedLink view="versions" query={{ policy_id: policyId }}>查看版本历史</RelatedLink>}
        <RelatedLink view="fetches" query={{ policy_id: policyId }}>查看采集记录</RelatedLink>
        <OfficialLink url={query.data.source_url}>打开官方来源</OfficialLink>
      </div>
      <div className="dc-body"><h3>原始正文</h3><pre>{query.data.body_text}</pre></div>
      {(offset > 0 || query.data.next_offset !== null) && <div className="dc-pager">
        <p>第 {offset + 1}–{query.data.next_offset ?? query.data.body_length} 字符，共 {query.data.body_length} 字符</p>
        <div>
          <button className="dc-button" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 8000))}><ChevronLeft size={15} aria-hidden />上一段</button>
          <button className="dc-button" disabled={query.data.next_offset === null} onClick={() => setOffset(query.data?.next_offset ?? offset)}>下一段<ChevronRight size={15} aria-hidden /></button>
        </div>
      </div>}
      <p className="dc-notice">{query.data.notice}</p>
    </article>}</QueryState>
  </Panel>
}

function PolicyRow({ item, onSelect }: { item: Policy; onSelect: (id: string) => void }) {
  return <tr>
    <th scope="row"><button className="dc-text-button dc-policy-title" onClick={() => onSelect(item.policy_id)}>{item.title}</button>
      <small><code>{item.policy_id}</code></small></th>
    <td>{item.region || <span className="dc-muted">未标注</span>}</td>
    <td>{item.issuer || '—'}</td>
    <td>{item.published_on || '—'}</td>
    <td><Badge tone="accent">v{item.version}</Badge></td>
    <td><Hash value={item.content_sha256} label="正文内容哈希" /></td>
  </tr>
}

export default function PoliciesPage() {
  const [keyword, setKeyword] = useState('')
  const [filters, setFilters] = useState({ keyword: '', region: '', offset: 0 })
  const [selected, setSelected] = useState(routeParam('policy_id'))
  const [selectedVersion, setSelectedVersion] = useState(() => {
    const version = Number(routeParam('version'))
    return Number.isSafeInteger(version) && version > 0 ? version : undefined
  })
  const query = useApi<Policies>(withQuery('/policies', { ...filters, limit }))
  const regions = useApi<Policies>('/policies?limit=1')
  return <>
    <PageHeading eyebrow="Policy Library" title="政策库" description="搜索版本化政策记录，查看正文、官方来源与内容哈希。" />
    <div className="dc-stack">
      <Panel>
        <form className="dc-filter-bar" onSubmit={event => { event.preventDefault(); setFilters(current => ({ ...current, keyword: keyword.trim(), offset: 0 })) }}>
          <label className="dc-search"><Search size={16} aria-hidden /><span className="dc-sr-only">搜索标题或正文</span>
            <input value={keyword} onChange={event => setKeyword(event.target.value)} placeholder="搜索标题或正文关键词" maxLength={200} /></label>
          <label><span>地区</span><select value={filters.region} onChange={event => setFilters(current => ({ ...current, region: event.target.value, offset: 0 }))}>
            <option value="">全部地区</option><option value="__unassigned__">未标注地区</option>
            {regions.data?.regions.map(item => <option key={item} value={item}>{item}</option>)}
          </select></label>
          <button className="dc-button dc-primary" type="submit">搜索</button>
          <button className="dc-button" type="button" onClick={() => { setKeyword(''); setFilters({ keyword: '', region: '', offset: 0 }) }}>清除筛选</button>
        </form>
      </Panel>
      <Panel>
        <SectionHeading title="政策记录" action={<Badge tone="accent">{query.data?.total ?? '—'} 条</Badge>} />
        <QueryState {...query}>
          {query.data?.items.length ? <DataTable label="政策列表" headers={['政策名称', '地区', '发布机构', '发布日期', '版本', '内容哈希']}>
            {query.data.items.map(item => <PolicyRow key={item.policy_id} item={item} onSelect={id => {
              setSelectedVersion(undefined)
              setSelected(id)
            }} />)}
          </DataTable> : <Empty title="没有匹配的政策记录" description="尝试减少关键词或选择全部地区。" />}
        </QueryState>
        {query.data && <PageControls offset={filters.offset} limit={limit} total={query.data.total} onOffset={offset => setFilters(current => ({ ...current, offset }))} />}
        {query.data?.notice && <p className="dc-notice">{query.data.notice}</p>}
      </Panel>
      {selected && <PolicyDetailPanel key={selected + ':' + (selectedVersion ?? 'latest')}
        policyId={selected} version={selectedVersion} onClose={() => setSelected('')} />}
    </div>
  </>
}
