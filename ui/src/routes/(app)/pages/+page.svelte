<script lang="ts">
  import { onMount } from 'svelte'

  import { pages as pagesApi, roles as rolesApi, users as usersApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { AdminPage, Role, User, UserDetail } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let allPages = $state<AdminPage[]>([])
  let allRoles = $state<Role[]>([])
  let allUsers = $state<User[]>([])
  let loading = $state(true)

  // ─── User access pane ───
  let selectedUserId = $state<number | null>(null)
  let userDetail = $state<UserDetail | null>(null)
  let userPageIds = $state<number[]>([])
  let userBusy = $state(false)

  // ─── Role access pane ───
  let selectedRoleId = $state<number | null>(null)
  let rolePageIds = $state<number[]>([])
  let roleBusy = $state(false)

  async function loadAll() {
    loading = true
    try {
      const [p, r, u] = await Promise.all([
        pagesApi.list(),
        rolesApi.list(),
        usersApi.list({ limit: 200 }),
      ])
      allPages = p
      allRoles = r
      allUsers = u.items
      // Авто-выбор первого
      if (allUsers.length > 0) selectedUserId = allUsers[0].id
      if (allRoles.length > 0) selectedRoleId = allRoles[0].id
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadUser(id: number) {
    try {
      userDetail = await usersApi.get(id)
      userPageIds = [...userDetail.direct_page_ids]
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function loadRole(id: number) {
    const r = allRoles.find((x) => x.id === id)
    rolePageIds = r ? r.pages.map((p) => p.id) : []
  }

  $effect(() => {
    if (selectedUserId !== null) loadUser(selectedUserId)
  })

  $effect(() => {
    if (selectedRoleId !== null) loadRole(selectedRoleId)
  })

  onMount(loadAll)

  async function saveUserAccess() {
    if (selectedUserId === null) return
    userBusy = true
    try {
      await usersApi.update(selectedUserId, { page_ids: userPageIds })
      showToast('success', 'User pages saved')
      await loadUser(selectedUserId)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      userBusy = false
    }
  }

  async function saveRoleAccess() {
    if (selectedRoleId === null) return
    roleBusy = true
    try {
      await rolesApi.update(selectedRoleId, { page_ids: rolePageIds })
      showToast('success', 'Role pages saved')
      // refresh allRoles so subsequent role-switch sees updated data
      allRoles = await rolesApi.list()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      roleBusy = false
    }
  }
</script>

<div class="space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Page Access</h1>
    <p class="mt-1 text-sm text-slate-500">User and role page assignment.</p>
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else}
    <div class="grid gap-6 lg:grid-cols-2">
      <!-- USER ACCESS -->
      <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 class="text-lg font-medium text-slate-900">User access</h2>

        <label for="user_pick" class="mt-4 block text-sm font-medium text-slate-700">User</label>
        <select id="user_pick" bind:value={selectedUserId}
                class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
          {#each allUsers as u}
            <option value={u.id}>{u.username}{u.full_name ? ` (${u.full_name})` : ''}</option>
          {/each}
        </select>

        <div class="mt-4 space-y-2 rounded-md bg-slate-50 p-4">
          {#each allPages as p}
            <label class="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                value={p.id}
                checked={userPageIds.includes(p.id)}
                onchange={(e) => {
                  if (e.currentTarget.checked) userPageIds = [...userPageIds, p.id]
                  else userPageIds = userPageIds.filter((id) => id !== p.id)
                }}
                class="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
              />
              <div class="flex-1">
                <div class="font-medium text-slate-900">{p.name}</div>
                <div class="font-mono text-xs text-slate-500">{p.path}</div>
              </div>
            </label>
          {/each}
        </div>

        <div class="mt-4 flex items-center gap-3">
          <button type="button" onclick={saveUserAccess} disabled={userBusy}
                  class="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {userBusy ? 'Saving…' : 'Save user access'}
          </button>
          <p class="text-xs text-slate-400">
            Это <b>дополнительные</b> страницы поверх тех, что юзер получает через роли.
          </p>
        </div>
      </section>

      <!-- ROLE ACCESS -->
      <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 class="text-lg font-medium text-slate-900">Role access</h2>

        <label for="role_pick" class="mt-4 block text-sm font-medium text-slate-700">Role</label>
        <select id="role_pick" bind:value={selectedRoleId}
                class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
          {#each allRoles as r}
            <option value={r.id}>{r.name}{r.is_system ? ' (system)' : ''}</option>
          {/each}
        </select>

        <div class="mt-4 space-y-2 rounded-md bg-slate-50 p-4">
          {#each allPages as p}
            <label class="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                value={p.id}
                checked={rolePageIds.includes(p.id)}
                onchange={(e) => {
                  if (e.currentTarget.checked) rolePageIds = [...rolePageIds, p.id]
                  else rolePageIds = rolePageIds.filter((id) => id !== p.id)
                }}
                class="mt-0.5 h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
              />
              <div class="flex-1">
                <div class="font-medium text-slate-900">{p.name}</div>
                <div class="font-mono text-xs text-slate-500">{p.path}</div>
              </div>
            </label>
          {/each}
        </div>

        <div class="mt-4 flex items-center gap-3">
          <button type="button" onclick={saveRoleAccess} disabled={roleBusy}
                  class="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {roleBusy ? 'Saving…' : 'Save role access'}
          </button>
          <p class="text-xs text-slate-400">
            Влияет на <b>всех</b> юзеров с этой ролью.
          </p>
        </div>
      </section>
    </div>
  {/if}
</div>
