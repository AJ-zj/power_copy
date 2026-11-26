import { Building2, Globe2, ShieldCheck } from 'lucide-react'
import { Badge, Empty, PageHeading, Panel } from '../../components/ui'
import { useApi } from '../../hooks/useApi'
import { dataLink, DataTable, ItemStatus, OfficialLink, QueryState, SectionHeading } from './common'
import type { Sources } from './models'

export default function SourcesPage() {
  const query = useApi<Sources>('/sources')
  const data = query.data
  return <>
    <PageHeading eyebrow="Source Registry" title="数据源注册表" description="核对采集来源、域名白名单与待审核候选。" />
    <QueryState {...query}>
      {data && <div className="dc-stack">
        <Panel>
          <SectionHeading title="已注册数据源" action={<Badge tone="accent">{data.items.length} 个来源</Badge>} />
          {data.items.length ? <div className="dc-source-grid">
            {data.items.map(source => <article className="dc-source" key={source.source_key}>
              <div className="dc-item-head">
                <span className="dc-icon"><Building2 size={18} aria-hidden /></span>
                <Badge tone={source.enabled ? 'success' : 'neutral'}>{source.enabled ? '已启用' : '已停用'}</Badge>
              </div>
              <h3>{source.name}</h3>
              <code className="dc-muted">{source.source_key}</code>
              <div className="dc-source-domain">
                <ShieldCheck size={15} aria-hidden /> <span>域名白名单</span> <code>{source.allowed_host}</code>
              </div>
              <div className="dc-targets">
                {source.targets.map(target => <div key={target.target_key} className="dc-target">
                  <Globe2 size={15} aria-hidden />
                  <div>
                    <OfficialLink url={target.url}>{target.target_key}</OfficialLink>
                    <div className="dc-target-meta">{target.content_kind} · {target.approved ? '已批准' : '待审核'}</div>
                  </div>
                </div>)}
                {!source.targets.length && <p className="dc-muted">暂无采集目标</p>}
              </div>
            </article>)}
          </div> : <Empty title="暂无注册数据源" />}
        </Panel>
        <Panel>
          <SectionHeading title="待审核候选" description="从已保存的目录快照中发现的目标。" action={<Badge tone="warning">{data.candidates.length} 条候选</Badge>} />
          {data.candidates.length ? <DataTable label="候选采集目标" headers={['候选目标', '来源', '目录档案', '状态']}>
            {data.candidates.map(candidate => <tr key={`${candidate.index_fetch_id}-${candidate.url}`}>
              <th scope="row"><OfficialLink url={candidate.url}>{candidate.title || candidate.url}</OfficialLink></th>
              <td><code>{candidate.source_key}</code></td>
              <td><a className="dc-link" href={dataLink('archive', { fetch_id: candidate.index_fetch_id })}><code>{candidate.index_fetch_id}</code></a></td>
              <td><ItemStatus state={candidate.status} /></td>
            </tr>)}
          </DataTable> : <Empty title="暂无可审核的候选目标" />}
        </Panel>
      </div>}
    </QueryState>
  </>
}
