import { useState } from 'react'
import { FileCode2, FileText, SquareArrowOutUpRight, X } from 'lucide-react'
import { apiPath } from '../../api/client'
import { Badge, Empty, PageHeading, Panel } from '../../components/ui'
import { useApi } from '../../hooks/useApi'
import { DataTable, Hash, OfficialLink, QueryState, routeParam, SectionHeading, sizeLabel, timeLabel } from './common'
import type { Archive, ArchiveItem } from './models'

function Provenance({ item }: { item: ArchiveItem }) {
  if (item.provenance === 'official_capture') return <Badge tone="success">官方采集</Badge>
  if (item.provenance === 'synthetic_fixture') return <Badge tone="warning">验收样本 · 合成文件</Badge>
  if (item.provenance === 'manual_offline') return <Badge tone="neutral">人工离线收录</Badge>
  return <Badge tone="warning">来源待核验</Badge>
}

function ArchiveDetail({ fetchId, onClose }: { fetchId: string; onClose: () => void }) {
  const path = '/archive/' + encodeURIComponent(fetchId)
  const query = useApi<ArchiveItem>(path)
  const item = query.data
  return <Panel className="dc-detail-panel">
    <div className="dc-detail-close"><button className="dc-icon-button" onClick={onClose} aria-label="关闭档案详情"><X size={17} aria-hidden /></button></div>
    <QueryState {...query}>{item && <>
      <SectionHeading title="档案详情" description={item.filename} action={<Provenance item={item} />} />
      <dl className="dc-meta-grid">
        <div><dt>采集 ID</dt><dd><code>{item.fetch_id}</code></dd></div>
        <div><dt>数据源</dt><dd>{item.source_key}</dd></div>
        <div><dt>采集目标</dt><dd>{item.target_key}</dd></div>
        <div><dt>内容类型</dt><dd>{item.content_type || item.content_kind}</dd></div>
        <div><dt>档案大小</dt><dd>{sizeLabel(item.body_size)}</dd></div>
        <div><dt>采集时间（北京时间）</dt><dd>{timeLabel(item.retrieved_at)}</dd></div>
        <div><dt>原始数据哈希</dt><dd><Hash value={item.raw_sha256} /></dd></div>
        <div><dt>采集地址</dt><dd><OfficialLink url={item.source_url} /></dd></div>
      </dl>
      {item.preview_kind && <div className="dc-body">
        <div className="dc-item-head">
          <h3>{item.preview_kind.toUpperCase()} 文本预览</h3>
          <a className="dc-link" href={apiPath(path + '/preview')} target="_blank" rel="noreferrer">
            单独打开<SquareArrowOutUpRight size={13} aria-hidden />
          </a>
        </div>
        <iframe title={item.filename + ' 的原始档案预览'} src={apiPath(path + '/preview')} sandbox="" referrerPolicy="no-referrer" className="dc-preview" />
      </div>}
    </>}</QueryState>
  </Panel>
}

export default function ArchivePage() {
  const [selected, setSelected] = useState(routeParam('fetch_id'))
  const query = useApi<Archive>('/archive')
  return <>
    <PageHeading eyebrow="Raw Archive" title="原始档案" description="打开已登记的 HTML 或 PDF 档案，核对采集元信息与原始数据哈希。" />
    <div className="dc-stack">
      <Panel>
        <SectionHeading title="已登记文件" action={<Badge tone="accent">{query.data?.items.length ?? '—'} 个文件</Badge>} />
        <QueryState {...query}>
          {query.data?.items.length ? <DataTable label="原始档案文件" headers={['档案及目标', '类型', '采集时间（北京时间）', '大小', '档案来源', '操作']}>
            {query.data.items.map(item => <tr key={item.fetch_id} data-selected={selected === item.fetch_id}>
              <th scope="row"><div className="dc-file-title">
                {item.preview_kind === 'pdf' ? <FileText size={17} aria-hidden /> : <FileCode2 size={17} aria-hidden />}
                <div><strong>{item.target_key}</strong><small><code>{item.filename}</code></small></div>
              </div></th>
              <td><Badge>{(item.preview_kind ?? item.content_kind).toUpperCase()}</Badge></td>
              <td>{timeLabel(item.retrieved_at)}</td>
              <td>{sizeLabel(item.body_size)}</td>
              <td><Provenance item={item} /></td>
              <td><button type="button" className="dc-text-button" onClick={() => setSelected(item.fetch_id)} aria-pressed={selected === item.fetch_id}>
                {item.preview_kind ? '查看预览' : '查看详情'}
              </button></td>
            </tr>)}
          </DataTable> : <Empty title="暂无可读取的原始档案" />}
        </QueryState>
      </Panel>
      {Boolean(query.data?.unavailable.length) && <Panel>
        <SectionHeading title="无法读取的档案" />
        <DataTable label="无法读取的档案" headers={['采集 ID', '原因']}>
          {query.data?.unavailable.map(item => <tr key={item.fetch_id}>
            <th scope="row"><code>{item.fetch_id}</code></th><td>{item.message} <small><code>{item.code}</code></small></td>
          </tr>)}
        </DataTable>
      </Panel>}
      {selected && <ArchiveDetail key={selected} fetchId={selected} onClose={() => setSelected('')} />}
    </div>
  </>
}
