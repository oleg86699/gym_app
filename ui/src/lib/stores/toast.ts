import { writable } from 'svelte/store'

export type ToastKind = 'info' | 'success' | 'warning' | 'error'

export interface Toast {
  id: number
  kind: ToastKind
  message: string
}

let counter = 0
export const toasts = writable<Toast[]>([])

export function showToast(kind: ToastKind, message: string, ttl = 4000) {
  const id = ++counter
  toasts.update((arr) => [...arr, { id, kind, message }])
  if (ttl > 0) {
    setTimeout(() => dismissToast(id), ttl)
  }
  return id
}

export function dismissToast(id: number) {
  toasts.update((arr) => arr.filter((t) => t.id !== id))
}
