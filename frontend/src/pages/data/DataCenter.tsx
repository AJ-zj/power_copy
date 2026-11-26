import ArchivePage from './ArchivePage'
import FetchRecordsPage from './FetchRecordsPage'
import IngestionPage from './IngestionPage'
import PoliciesPage from './PoliciesPage'
import SourcesPage from './SourcesPage'
import VersionsPage from './VersionsPage'
import type { DataView } from './models'
import './data.css'

const pages = {
  sources: SourcesPage,
  ingestion: IngestionPage,
  policies: PoliciesPage,
  versions: VersionsPage,
  fetches: FetchRecordsPage,
  archive: ArchivePage,
}

export default function DataCenter({ view }: { view: DataView }) {
  const Page = pages[view]
  return <Page key={location.hash} />
}
