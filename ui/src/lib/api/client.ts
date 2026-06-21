/**
 * Тонкий fetch-клиент к /admin/api.
 * Авторизация — через cookie (httpOnly) которая выставляется на /auth/login.
 */

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `API error ${status}`)
    this.status = status
    this.detail = detail
  }
}

// Глобальный hook на 401 — регистрирует user-store при инициализации.
// Здесь делаем поздний bind чтобы избежать circular import client ↔ user store.
let onUnauthorized: (() => void) | null = null
export function setOnUnauthorized(fn: () => void): void {
  onUnauthorized = fn
}

// Эндпоинты, у которых 401 — это нормальный ответ-«не пустили», а не «сессия
// истекла». На них НЕ триггерим глобальный redirect:
//   - /auth/login и /auth/register — юзер сам на /login, ему нужно увидеть error toast
//   - /auth/me — мы сами проверяем сессию из (app)/+layout.ts; если 401 здесь,
//     layout делает throw redirect(307, '/login?...') сам. Глобальный goto тут
//     создаёт race condition с layout-редиректом → SvelteKit падает в default
//     error.svelte (status=-1) вместо чистого перехода на /login.
const _AUTH_ENDPOINTS = [
  '/admin/api/auth/login',
  '/admin/api/auth/register',
  '/admin/api/auth/me',
]

function _isAuthEndpoint(url: string): boolean {
  return _AUTH_ENDPOINTS.some((p) => url.includes(p))
}

async function handle(res: Response) {
  if (res.status === 204) return null
  const ct = res.headers.get('Content-Type') ?? ''
  const body = ct.includes('application/json') ? await res.json() : await res.text()
  if (!res.ok) {
    const detail =
      typeof body === 'object' && body && 'detail' in body ? String(body.detail) : undefined
    if (res.status === 401 && !_isAuthEndpoint(res.url)) {
      // Сессия истекла — триггерим redirect на /login. ApiError всё равно
      // бросаем, чтобы вызывающий код мог корректно завершить свою цепочку
      // (но toast будет с friendly-сообщением, а не «API error 401»).
      try { onUnauthorized?.() } catch { /* ignore */ }
      throw new ApiError(401, body, 'Session expired — redirecting to login…')
    }
    throw new ApiError(res.status, body, detail)
  }
  return body
}

async function request<T = unknown>(
  method: string,
  path: string,
  options: { json?: unknown; query?: Record<string, string | number | boolean | undefined> } = {},
): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (options.query) {
    for (const [k, v] of Object.entries(options.query)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v))
    }
  }

  const init: RequestInit = {
    method,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(options.json !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: options.json !== undefined ? JSON.stringify(options.json) : undefined,
  }
  const res = await fetch(url.toString(), init)
  return (await handle(res)) as T
}

async function upload<T = unknown>(
  path: string,
  formData: FormData,
  query?: Record<string, string | number | boolean | undefined>,
): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v))
    }
  }
  const res = await fetch(url.toString(), {
    method: 'POST',
    credentials: 'same-origin',
    body: formData,
    // НЕ ставим Content-Type — браузер сам выставит multipart boundary
  })
  return (await handle(res)) as T
}

export const api = {
  get: <T = unknown>(path: string, query?: Record<string, string | number | boolean | undefined>) =>
    request<T>('GET', path, { query }),
  post: <T = unknown>(path: string, json?: unknown) => request<T>('POST', path, { json }),
  put: <T = unknown>(path: string, json?: unknown) => request<T>('PUT', path, { json }),
  patch: <T = unknown>(path: string, json?: unknown) => request<T>('PATCH', path, { json }),
  del: <T = unknown>(path: string) => request<T>('DELETE', path),
  upload,
}
