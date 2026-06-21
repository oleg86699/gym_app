import { redirect } from '@sveltejs/kit'

export const ssr = false
export const prerender = false

export function load() {
  throw redirect(307, '/dashboard')
}
