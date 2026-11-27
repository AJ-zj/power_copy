import { useState, type ReactNode } from 'react'
import {
  Activity, Archive, ArrowUpRight, AudioLines, Blocks, BookOpen, ChevronRight, Database,
  FileClock, FlaskConical, GitBranch, Globe2, LayoutDashboard, Layers3, Menu, Network,
  PenLine, Radio, ScanLine, Wrench, X, Zap,
} from 'lucide-react'
import { useApi } from '../hooks/useApi'
import type { RuntimeInfo } from '../types/workbench'

const primary = [
  { path: '/overview', label: '项目总览', icon: LayoutDashboard },
  { path: '/rag', label: 'RAG Studio', icon: Layers3 },
  { path: '/content', label: 'Content Studio', icon: PenLine },
  { path: '/playground', label: 'Agent Playground', icon: FlaskConical },
  { path: '/observability', label: '可观测中心', icon: Activity },
  { path: '/tools', label: 'Tool Registry', icon: Wrench },
]

const data = [
  { path: '/data/sources', label: '数据源注册', icon: Globe2 },
  { path: '/data/ingestion', label: '采集流水线', icon: GitBranch },
  { path: '/data/policies', label: '政策库', icon: BookOpen },
  { path: '/data/versions', label: '版本历史', icon: FileClock },
  { path: '/data/fetches', label: '采集事件', icon: Radio },
  { path: '/data/archive', label: '原始档案', icon: Archive },
]

const roadmap = [
  { path: '/roadmap/memory', label: 'Memory Center', icon: Database },
  { path: '/roadmap/context', label: 'Context Engineering', icon: Network },
  { path: '/roadmap/skills', label: 'Skills Library', icon: Blocks },
  { path: '/roadmap/evaluation', label: 'Evaluation Center', icon: ScanLine },
]

const groups = [
  { title: '工作台', links: primary },
  { title: '数据中心', links: data },
  { title: '后续研发', links: roadmap },
]

function currentPath(route: string) {
  if (route === '/roadmap/rag') return '/rag'
  if (route === '/roadmap/content') return '/content'
  return route
}

export function routeLabel(route: string) {
  return groups.flatMap(group => group.links).find(link => link.path === currentPath(route))?.label
}

export function WorkbenchLayout({ route, children }: { route: string; children: ReactNode }) {
  const [showNav, setShowNav] = useState(false)
  const backend = useApi<RuntimeInfo>('/runtime')
  const currentRoute = currentPath(route)
  const label = routeLabel(currentRoute) ?? '页面'
  const liveStatus = backend.loading ? '正在连接' : backend.error ? '后端离线' : '本地服务在线'
  const dataRoute = currentRoute.startsWith('/data/')
  const roadmapRoute = currentRoute.startsWith('/roadmap/')

  return <div className="app-shell">
    <a className="skip-link" href="#main-content" onClick={event => {
      event.preventDefault()
      document.getElementById('main-content')?.focus()
    }}>跳转到主要内容</a>
    <aside className={`sidebar ${showNav ? 'sidebar--open' : ''}`} id="workbench-nav">
      <a href="#/overview" className="brand" onClick={() => setShowNav(false)}>
        <span className="brand-mark"><Zap size={20} aria-hidden="true" /></span>
        <span><strong>PowerCopy <small>V2</small></strong><span>Agent 工作台</span></span>
      </a>
      <nav aria-label="工作台主导航">
        {groups.map(group => <div className="nav-group" key={group.title}>
          <p>{group.title}</p>
          <ul>{group.links.map(({ path, label, icon: Icon }) =>
            <li key={path}><a href={`#${path}`} aria-current={currentRoute === path ? 'page' : undefined}
              className={path.startsWith('/roadmap/') ? 'nav-future' : undefined}
              onClick={() => setShowNav(false)}>
              <Icon size={16} aria-hidden="true" /><span>{label}</span>
              {currentRoute === path && <span className="nav-active-dot" aria-hidden="true" />}
            </a></li>,
          )}</ul>
        </div>)}
      </nav>
      <div className="sidebar-footer"><span className="footer-mark"><AudioLines size={16} aria-hidden="true" /></span>
        <div><strong>昆吾电力</strong><small>PowerCopy 研发环境</small></div>
      </div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <div className="breadcrumbs">
          <button className="icon-button nav-toggle" aria-controls="workbench-nav" aria-expanded={showNav}
            aria-label={showNav ? '关闭导航' : '打开导航'} onClick={() => setShowNav(!showNav)}>
            {showNav ? <X size={19} /> : <Menu size={19} />}
          </button>
          <span>{dataRoute ? '数据中心' : roadmapRoute ? '后续研发' : '工作台'}</span>
          <ChevronRight size={13} aria-hidden="true" /><strong>{label}</strong>
        </div>
        <div className="topbar-actions">
          <button className={`connection ${backend.error ? 'connection--offline' : ''}`}
            onClick={backend.reload} title="重新检查连接">
            <i aria-hidden="true" />{liveStatus}
          </button>
          <a className="topbar-link" href="#/tools"><Wrench size={15} aria-hidden="true" /> 工具 <ArrowUpRight size={12} aria-hidden="true" /></a>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="page-content">{children}</main>
    </div>
  </div>
}
