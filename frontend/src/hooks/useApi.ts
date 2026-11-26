import { useCallback, useEffect, useState } from 'react'
import { get } from '../api/client'

interface RequestState<T> {
  key: string
  data?: T
  error: string | null
}

export function useApi<T>(path: string | null) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState<RequestState<T>>()
  const reload = useCallback(() => setRevision(value => value + 1), [])
  const key = `${revision}:${path}`
  useEffect(() => {
    if (!path) return
    const request = new AbortController()
    get<T>(path, request.signal).then(
      data => {
        if (!request.signal.aborted) setResult({ key, data, error: null })
      },
      error => {
        if (!request.signal.aborted) setResult({ key, error: error.message })
      },
    )
    return () => request.abort()
  }, [key, path])
  const current = result?.key === key ? result : undefined
  return { data: current?.data, error: current?.error ?? null, loading: Boolean(path && !current), reload }
}
