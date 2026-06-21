import { goto } from '$app/navigation'
import { writable } from 'svelte/store'

import { auth } from '$lib/api/admin'
import { ApiError, setOnUnauthorized } from '$lib/api/client'
import type { MeResponse } from '$lib/api/types'
import { showToast } from '$lib/stores/toast'

export const currentUser = writable<MeResponse | null>(null)
export const userLoaded = writable<boolean>(false)

// Дедуп — несколько параллельных запросов словивших 401 не должны делать
// N goto подряд (последний из них всё равно проиграет race) и спамить toast.
let redirectingToLogin = false

function redirectToLogin(): void {
  if (typeof window === 'undefined') return
  if (redirectingToLogin) return
  // Уже на /login — не перенаправляем (это вызовет цикл / лишний переход)
  if (window.location.pathname.startsWith('/login')) return
  redirectingToLogin = true
  currentUser.set(null)
  // Один friendly toast вместо N сырых «API error 401» от callsites.
  try {
    showToast('warning', 'Сессия истекла — нужно войти заново')
  } catch { /* toaster ещё не смонтировался — ничего страшного */ }
  const next = encodeURIComponent(window.location.pathname + window.location.search)
  // goto а не window.location — без перезагрузки страницы
  goto(`/login?next=${next}`, { replaceState: true, invalidateAll: true })
    .finally(() => { redirectingToLogin = false })
}

// Регистрируем глобальный 401-handler один раз при первом импорте модуля.
setOnUnauthorized(redirectToLogin)

export async function loadCurrentUser(): Promise<MeResponse | null> {
  try {
    const me = await auth.me()
    currentUser.set(me)
    return me
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      // redirectToLogin уже сработал глобально (см. setOnUnauthorized выше),
      // здесь просто сообщаем layout-у что user-а нет.
      currentUser.set(null)
      return null
    }
    throw e
  } finally {
    userLoaded.set(true)
  }
}

export function hasPageAccess(user: MeResponse | null, path: string): boolean {
  if (!user) return false
  if (user.is_super_admin) return true
  if (user.accessible_pages.includes('*')) return true
  // Грант страницы покрывает её под-роуты: /batches → /batches/11, /batches/.../...
  return user.accessible_pages.some((p) => path === p || path.startsWith(p + '/'))
}

export function hasPermission(user: MeResponse | null, code: string): boolean {
  if (!user) return false
  if (user.is_super_admin) return true
  if (user.permissions.includes('*')) return true
  return user.permissions.includes(code)
}

/** Куда вести юзера после логина: /dashboard если доступен, иначе первая
 *  доступная страница (для supplier-а это /portal). */
export function landingPath(user: MeResponse | null): string {
  if (!user) return '/login'
  if (user.is_super_admin || user.accessible_pages.includes('/dashboard')) return '/dashboard'
  return user.accessible_pages[0] ?? '/dashboard'
}
