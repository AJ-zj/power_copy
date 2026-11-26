const prefix = '/api/workbench'

export const apiPath = (path: string) => prefix + path

async function request<T>(path: string, options: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiPath(path), options)
  } catch (error) {
    if (options.signal?.aborted) throw error
    throw new Error('无法连接工作台 API。请确认后端服务已启动。')
  }
  if (!response.headers.get('content-type')?.includes('application/json')) {
    throw new Error('工作台 API 暂不可用。请确认后端服务已启动。')
  }
  const payload = await response.json()
  if (!response.ok || !payload || !('data' in payload)) {
    throw new Error(payload?.error?.message ?? `请求失败（HTTP ${response.status}）`)
  }
  return payload.data as T
}

export const get = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal })
export const post = <T>(path: string, body: unknown, signal?: AbortSignal) => request<T>(path, {
  method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})

export function officialLink(value?: string | null) {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined
  } catch {
    return undefined
  }
}
