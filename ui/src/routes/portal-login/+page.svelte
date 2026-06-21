<script lang="ts">
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { publicPortal } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { currentUser } from '$lib/stores/user'

  let error = $state<string | null>(null)
  let busy = $state(true)

  async function run() {
    const token = page.url.searchParams.get('token')
    if (!token) {
      error = 'Ссылка без токена'
      busy = false
      return
    }
    try {
      const res = await publicPortal.login(token)
      currentUser.set(res.user)
      await goto('/batches', { replaceState: true })
    } catch (e) {
      error = e instanceof ApiError && e.status === 410
        ? 'Ссылка недействительна или истекла'
        : `Ошибка входа (${e instanceof ApiError ? e.status : e})`
      busy = false
    }
  }

  $effect(() => { run() })
</script>

<div class="flex min-h-screen items-center justify-center bg-slate-50 px-4">
  <div class="w-full max-w-sm text-center">
    <h1 class="text-3xl font-semibold tracking-tight">
      gym<span class="text-brand-600">_app</span>
    </h1>
    {#if busy}
      <p class="mt-4 text-sm text-slate-500">Входим…</p>
    {:else if error}
      <div class="mt-6 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        {error}
      </div>
      <a href="/login" class="mt-4 inline-block text-sm text-brand-600 hover:underline">
        Войти по логину и паролю
      </a>
    {/if}
  </div>
</div>
