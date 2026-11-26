import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Badge, Empty, PageHeading, Panel } from './components/ui'
import { routeLabel, WorkbenchLayout } from './layouts/WorkbenchLayout'
import Overview from './pages/Overview'
import RagStudio from './pages/RagStudio'
import { Observability, Playground, ToolsPage } from './pages/Runtime'
import DataCenter from './pages/data/DataCenter'
import type { DataView } from './pages/data/models'
import type { TaskResult } from './types/workbench'
import './styles/workbench.css'

function App() {
  const [hash, setHash] = useState(location.hash)
  const [result, setResult] = useState<TaskResult | null>(null)
  const route = hash.slice(1).split('?')[0] || '/overview'
  const label = routeLabel(route)

  useEffect(() => {
    function change() {
      const next = location.hash
      setHash(next)
      window.scrollTo(0, 0)
    }
    window.addEventListener('hashchange', change)
    return () => window.removeEventListener('hashchange', change)
  }, [])

  useEffect(() => {
    document.title = `${label ?? '工作台'} · PowerCopy V2`
  }, [label])

  let page
  if (route === '/overview') page = <Overview />
  else if (route === '/rag' || route === '/roadmap/rag') page = <RagStudio />
  else if (route === '/playground') page = <Playground result={result} setResult={setResult} />
  else if (route === '/tools') page = <ToolsPage />
  else if (route === '/observability') page = <Observability result={result} />
  else if (label && route.startsWith('/data/')) page = <DataCenter view={route.slice(6) as DataView} />
  else if (label && route.startsWith('/roadmap/')) page = <>
    <PageHeading title={label}><Badge>后续研发</Badge></PageHeading>
    <Panel><Empty title="尚未接入" description="该模块已预留工作台入口。" /></Panel>
  </>
  else page = <><PageHeading title="页面不存在" /><a className="button" href="#/overview">返回项目总览</a></>

  return <WorkbenchLayout route={route}>{page}</WorkbenchLayout>
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>)
