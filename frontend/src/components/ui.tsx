import type { ReactNode } from 'react'
import { AlertCircle, Inbox, LoaderCircle } from 'lucide-react'

type Tone = 'neutral' | 'success' | 'warning' | 'error' | 'accent'

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: Tone }) {
  return <span className={`badge badge--${tone}`}>{children}</span>
}

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>{children}</section>
}

export function PageHeading({ eyebrow, title, description, children }: {
  eyebrow?: string; title: string; description?: string; children?: ReactNode
}) {
  return <header className="page-heading">
    <div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1>{title}</h1>{description && <p className="page-description">{description}</p>}
    </div>
    {children && <div className="heading-actions">{children}</div>}
  </header>
}

export function Empty({ title, description }: { title: string; description?: string }) {
  return <div className="state-block">
    <span className="state-mark"><Inbox size={20} aria-hidden="true" /></span>
    <strong>{title}</strong>{description && <p>{description}</p>}
  </div>
}

export function Loading() {
  return <div className="state-block" role="status">
    <LoaderCircle size={22} className="spinner" aria-hidden="true" /> 正在读取…
  </div>
}

export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="state-block state-error" role="alert">
    <AlertCircle size={20} aria-hidden="true" /><strong>读取失败</strong><p>{message}</p>
    {retry && <button className="button" onClick={retry}>重试</button>}
  </div>
}
