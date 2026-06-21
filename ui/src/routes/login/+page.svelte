<script lang="ts">
  import { goto } from '$app/navigation'
  import { auth } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { currentUser, landingPath } from '$lib/stores/user'

  let username = $state('')
  let password = $state('')
  let busy = $state(false)
  let error = $state<string | null>(null)

  async function submit(e: SubmitEvent) {
    e.preventDefault()
    error = null
    busy = true
    try {
      const res = await auth.login(username, password)
      currentUser.set(res.user)
      await goto(landingPath(res.user), { replaceState: true })
    } catch (e) {
      if (e instanceof ApiError) {
        error = e.status === 401 ? 'Invalid username or password' : `Error ${e.status}`
      } else {
        error = String(e)
      }
    } finally {
      busy = false
    }
  }
</script>

<div class="flex min-h-screen items-center justify-center bg-slate-50 px-4">
  <div class="w-full max-w-sm">
    <div class="mb-8 text-center">
      <h1 class="text-3xl font-semibold tracking-tight">
        gym<span class="text-brand-600">_app</span>
      </h1>
      <p class="mt-1 text-sm text-slate-500">Sign in to admin panel</p>
    </div>

    <form onsubmit={submit} class="space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <div>
        <label for="username" class="block text-sm font-medium text-slate-700">Username</label>
        <input
          id="username"
          type="text"
          bind:value={username}
          required
          autocomplete="username"
          autofocus
          class="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        />
      </div>

      <div>
        <label for="password" class="block text-sm font-medium text-slate-700">Password</label>
        <input
          id="password"
          type="password"
          bind:value={password}
          required
          autocomplete="current-password"
          class="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
        />
      </div>

      {#if error}
        <div class="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      {/if}

      <button
        type="submit"
        disabled={busy || !username || !password}
        class="w-full rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-brand-700 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-300"
      >
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  </div>
</div>
