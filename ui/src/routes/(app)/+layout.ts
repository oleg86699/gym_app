import { redirect } from '@sveltejs/kit'

import { loadCurrentUser } from '$lib/stores/user'

export const ssr = false
export const prerender = false

export async function load({ url }: { url: URL }) {
  const user = await loadCurrentUser()
  if (!user) {
    const next = encodeURIComponent(url.pathname + url.search)
    throw redirect(307, `/login?next=${next}`)
  }
  return { user }
}
