<script lang="ts">
  import { auth } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { showToast } from '$lib/stores/toast'
  import { currentUser, loadCurrentUser } from '$lib/stores/user'

  let user = $derived($currentUser)

  // Password change
  let currentPwd = $state('')
  let newPwd = $state('')
  let confirmPwd = $state('')
  let pwdBusy = $state(false)

  // Email change
  let newEmail = $state('')
  let emailPwd = $state('')
  let emailBusy = $state(false)

  async function submitPassword(e: SubmitEvent) {
    e.preventDefault()
    if (newPwd !== confirmPwd) {
      showToast('error', "New passwords don't match")
      return
    }
    pwdBusy = true
    try {
      await auth.changePassword(currentPwd, newPwd)
      showToast('success', 'Password changed')
      currentPwd = ''
      newPwd = ''
      confirmPwd = ''
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      pwdBusy = false
    }
  }

  async function submitEmail(e: SubmitEvent) {
    e.preventDefault()
    emailBusy = true
    try {
      await auth.changeEmail(emailPwd, newEmail)
      showToast('success', 'Email updated')
      newEmail = ''
      emailPwd = ''
      await loadCurrentUser()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      emailBusy = false
    }
  }
</script>

<div class="space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Profile</h1>
    <p class="mt-1 text-sm text-slate-500">Your account.</p>
  </div>

  <div class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
    <dl class="grid gap-3 text-sm sm:grid-cols-2">
      <div>
        <dt class="text-slate-500">Username</dt>
        <dd class="font-medium text-slate-900">{user?.username ?? '—'}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Email</dt>
        <dd class="font-medium text-slate-900">{user?.email ?? '—'}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Full name</dt>
        <dd class="font-medium text-slate-900">{user?.full_name ?? '—'}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Roles</dt>
        <dd class="font-medium text-slate-900">{user?.roles.map((r) => r.name).join(', ') || '—'}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Last login</dt>
        <dd class="font-medium text-slate-900">{user?.last_login_at ?? '—'}</dd>
      </div>
      <div>
        <dt class="text-slate-500">Created</dt>
        <dd class="font-medium text-slate-900">{user?.created_at ?? '—'}</dd>
      </div>
    </dl>
  </div>

  <div class="grid gap-6 md:grid-cols-2">
    <form onsubmit={submitPassword} class="space-y-3 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 class="text-lg font-medium text-slate-900">Change password</h2>
      <div>
        <label for="cur_pwd" class="block text-sm font-medium text-slate-700">Current password</label>
        <input id="cur_pwd" type="password" bind:value={currentPwd} required
               class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </div>
      <div>
        <label for="new_pwd" class="block text-sm font-medium text-slate-700">New password</label>
        <input id="new_pwd" type="password" bind:value={newPwd} required minlength="8"
               class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </div>
      <div>
        <label for="cnf_pwd" class="block text-sm font-medium text-slate-700">Confirm new password</label>
        <input id="cnf_pwd" type="password" bind:value={confirmPwd} required minlength="8"
               class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </div>
      <button type="submit" disabled={pwdBusy}
              class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
        {pwdBusy ? 'Saving…' : 'Change password'}
      </button>
    </form>

    <form onsubmit={submitEmail} class="space-y-3 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 class="text-lg font-medium text-slate-900">Change email</h2>
      <div>
        <label for="new_email" class="block text-sm font-medium text-slate-700">New email</label>
        <input id="new_email" type="email" bind:value={newEmail} required
               class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </div>
      <div>
        <label for="email_pwd" class="block text-sm font-medium text-slate-700">Current password</label>
        <input id="email_pwd" type="password" bind:value={emailPwd} required
               class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </div>
      <button type="submit" disabled={emailBusy}
              class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
        {emailBusy ? 'Saving…' : 'Update email'}
      </button>
    </form>
  </div>
</div>
