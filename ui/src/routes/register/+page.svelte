<script lang="ts">
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import { publicInvitations } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { PublicInvitationView } from '$lib/api/types'
  import { currentUser } from '$lib/stores/user'

  let token = $derived(page.url.searchParams.get('token') ?? '')

  let invite = $state<PublicInvitationView | null>(null)
  let lookupError = $state<string | null>(null)
  let loading = $state(true)

  // Form fields
  let username = $state('')
  let password = $state('')
  let confirmPassword = $state('')
  let email = $state('')
  let fullName = $state('')

  let submitting = $state(false)
  let submitError = $state<string | null>(null)

  async function load() {
    if (!token) {
      lookupError = 'No invitation token provided'
      loading = false
      return
    }
    loading = true
    try {
      invite = await publicInvitations.view(token)
      if (invite.email) email = invite.email
    } catch (e) {
      lookupError = e instanceof ApiError ? e.message : String(e)
    } finally {
      loading = false
    }
  }
  onMount(load)

  async function submit(e: SubmitEvent) {
    e.preventDefault()
    if (password !== confirmPassword) {
      submitError = "Passwords don't match"
      return
    }
    submitError = null
    submitting = true
    try {
      const res = await publicInvitations.accept(token, {
        username,
        password,
        email: email || undefined,
        full_name: fullName || undefined,
      })
      currentUser.set(res.user)
      await goto('/dashboard', { replaceState: true })
    } catch (e) {
      submitError = e instanceof ApiError ? e.message : String(e)
    } finally {
      submitting = false
    }
  }
</script>

<div class="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-12">
  <div class="w-full max-w-md">
    <div class="mb-8 text-center">
      <h1 class="text-3xl font-semibold tracking-tight">
        gym<span class="text-brand-600">_app</span>
      </h1>
      <p class="mt-1 text-sm text-slate-500">Accept invitation</p>
    </div>

    {#if loading}
      <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500 shadow-sm">
        Checking invitation…
      </div>
    {:else if lookupError}
      <div class="rounded-lg border border-red-200 bg-red-50 p-6 shadow-sm">
        <h2 class="text-lg font-medium text-red-800">Invitation invalid</h2>
        <p class="mt-1 text-sm text-red-700">{lookupError}</p>
        <p class="mt-3 text-xs text-red-600">
          Возможно ссылка истекла, отозвана или уже использована. Попроси новую у того, кто пригласил.
        </p>
        <a href="/login" class="mt-4 inline-block text-sm text-red-700 underline hover:text-red-900">
          Go to login
        </a>
      </div>
    {:else if invite}
      <div class="space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div class="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          <div>
            You've been invited
            {#if invite.invited_by_username}by <strong>@{invite.invited_by_username}</strong>{/if}
            {#if invite.group_name}to join group <strong>#{invite.group_name}</strong>{/if}.
          </div>
          <div class="mt-1 text-xs text-emerald-700">
            Expires {new Date(invite.expires_at).toLocaleString()}
          </div>
        </div>

        <form onsubmit={submit} class="space-y-3">
          <div>
            <label for="r_username" class="block text-sm font-medium text-slate-700">Username *</label>
            <input id="r_username" type="text" bind:value={username} required minlength="3"
                   autocomplete="username"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
          </div>

          <div>
            <label for="r_password" class="block text-sm font-medium text-slate-700">Password *</label>
            <input id="r_password" type="password" bind:value={password} required minlength="8"
                   autocomplete="new-password"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
          </div>

          <div>
            <label for="r_cnfpwd" class="block text-sm font-medium text-slate-700">Confirm password *</label>
            <input id="r_cnfpwd" type="password" bind:value={confirmPassword} required minlength="8"
                   autocomplete="new-password"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
          </div>

          <div>
            <label for="r_email" class="block text-sm font-medium text-slate-700">Email</label>
            <input id="r_email" type="email" bind:value={email}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
          </div>

          <div>
            <label for="r_fullname" class="block text-sm font-medium text-slate-700">Full name</label>
            <input id="r_fullname" type="text" bind:value={fullName} maxlength="255"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
          </div>

          {#if submitError}
            <div class="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {submitError}
            </div>
          {/if}

          <button type="submit" disabled={submitting || !username || !password}
                  class="w-full rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-brand-700 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-300">
            {submitting ? 'Creating account…' : 'Register & sign in'}
          </button>
        </form>
      </div>
    {/if}
  </div>
</div>
