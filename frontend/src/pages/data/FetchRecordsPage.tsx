import { useState } from 'react'
import { Badge, Empty, PageHeading, Panel } from '../../components/ui'
import { useApi } from '../../hooks/useApi'
import {
  DataTable, Hash, PageControls, QueryState, RelatedLink, routeParam, SectionHeading, timeLabel, withQuery,
} from './common'
import type { FetchRecord, Page } from './models'

const limit = 20

export default function FetchRecordsPage() {
  const policyId = routeParam('policy_id')
  const [offset, setOffset] = useState(0)
  const query = useApi<Page<FetchRecord>>(withQuery('/fetches', { policy_id: policyId, limit, offset }))
  return <>
    <PageHeading eyebrow="Fetch Records" title="采集记录" description="追溯每次采集关联的政策版本、原始数据哈希与保存文档。">
      {policyId && <RelatedLink view="fetches">查看全部采集记录</RelatedLink>}
    </PageHeading>
    <Panel>
      <SectionHeading title="采集事件" description={policyId ? '政策 ID ' + policyId : undefined}
        action={<Badge tone="accent">{query.data?.total ?? '—'} 条记录</Badge>} />
      <QueryState {...query}>
        {query.data?.items.length ? <DataTable label="采集事件" headers={['采集 ID', '政策及版本', '采集时间（北京时间）', '原始数据哈希', '关联文档']}>
          {query.data.items.map(item => <tr key={item.fetch_id}>
            <th scope="row"><code>{item.fetch_id}</code></th>
            <td><RelatedLink view="versions" query={{ policy_id: item.policy_id }}>{item.title || item.policy_id}</RelatedLink><small><Badge tone="accent">v{item.version}</Badge></small></td>
            <td>{timeLabel(item.retrieved_at)}</td>
            <td><Hash value={item.raw_sha256} /></td>
            <td><code>{item.document_id}</code><small><RelatedLink view="archive" query={{ fetch_id: item.fetch_id }}>查看档案</RelatedLink></small></td>
          </tr>)}
        </DataTable> : <Empty title="暂无采集记录" />}
      </QueryState>
      {query.data && <PageControls limit={limit} offset={offset} total={query.data.total} onOffset={setOffset} />}
    </Panel>
  </>
}
