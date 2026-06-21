<script lang="ts">
  import { onMount } from 'svelte'

  import { groups as groupsApi, roles as rolesApi, users as usersApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { Group, Role, User } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let items = $state<User[]>([])
  let groups = $state<Group[]>([])
  let roles = $state<Role[]>([])
  let loading = $state(true)
  let search = $state('')

  // Создание
  let createOpen = $state(false)
  let createBusy = $state(false)
  let newUsername = $state('')
  let newPassword = $state('')
  let newEmail = $state('')
  let newFullName = $state('')
  let newGroupId = $state<number | null>(null)
  let newRoleIds = $state<number[]>([])

  async function refresh() {
    loading = true
    try {
      const res = await usersApi.list({ search, limit: 100 })
      items = res.items
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadAux() {
    if ($currentUser?.is_super_admin) {
      try {
        groups = await groupsApi.list()
      } catch {
        groups = []
      }
      try {
        roles = await rolesApi.list()
      } catch {
        roles = []
      }
    }
  }

  onMount(async () => {
    await Promise.all([refresh(), loadAux()])
  })

  async function handleSearch(e: SubmitEvent) {
    e.preventDefault()
    await refresh()
  }

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    createBusy = true
    try {
      await usersApi.create({
        username: newUsername,
        password: newPassword,
        email: newEmail || undefined,
        full_name: newFullName || undefined,
        group_id: newGroupId ?? undefined,
        role_ids: newRoleIds.length ? newRoleIds : undefined,
      })
      showToast('success', `User ${newUsername} created`)
      createOpen = false
      newUsername = ''
      newPassword = ''
      newEmail = ''
      newFullName = ''
      newGroupId = null
      newRoleIds = []
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      createBusy = false
    }
  }

  async function handleDelete(user: User) {
    if (!confirm(`Delete user "${user.username}"?`)) return
    try {
      await usersApi.remove(user.id)
      showToast('success', `User ${user.username} deleted`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function handleToggleActive(user: User) {
    try {
      await usersApi.update(user.id, { is_active: !user.is_active })
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
</script>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Users</h1>
      <p class="mt-1 text-sm text-slate-500">{items.length} user(s)</p>
    </div>
    <button
      type="button"
      onclick={() => (createOpen = true)}
      class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700"
    >
      + New user
    </button>
  </div>

  <form onsubmit={handleSearch} class="flex gap-2">
    <input
      type="search"
      bind:value={search}
      placeholder="Search by username or email…"
      class="flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
    />
    <button class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
      Search
    </button>
  </form>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      Loading…
    </div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      No users found
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-4 py-2">Username</th>
            <th class="px-4 py-2">Email</th>
            <th class="px-4 py-2">Group</th>
            <th class="px-4 py-2">Roles</th>
            <th class="px-4 py-2">Status</th>
            <th class="px-4 py-2"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as user (user.id)}
            <tr>
              <td class="px-4 py-2 font-medium text-slate-900">
                <a href={`/users/${user.id}`} class="text-brand-600 hover:underline">{user.username}</a>
              </td>
              <td class="px-4 py-2 text-slate-600">{user.email ?? '—'}</td>
              <td class="px-4 py-2 text-slate-600">{user.group?.name ?? '—'}</td>
              <td class="px-4 py-2">
                <div class="flex flex-wrap gap-1">
                  {#each user.roles as r}
                    <span
                      class="rounded px-1.5 py-0.5 text-xs font-medium"
                      class:bg-amber-100={r.name === 'super_admin'}
                      class:text-amber-700={r.name === 'super_admin'}
                      class:bg-slate-100={r.name !== 'super_admin'}
                      class:text-slate-700={r.name !== 'super_admin'}
                    >
                      {r.name}
                    </span>
                  {/each}
                </div>
              </td>
              <td class="px-4 py-2">
                <button
                  type="button"
                  onclick={() => handleToggleActive(user)}
                  class="rounded-full px-2 py-0.5 text-xs font-medium"
                  class:bg-emerald-100={user.is_active}
                  class:text-emerald-700={user.is_active}
                  class:bg-slate-200={!user.is_active}
                  class:text-slate-600={!user.is_active}
                >
                  {user.is_active ? 'active' : 'inactive'}
                </button>
              </td>
              <td class="px-4 py-2 text-right">
                <a href={`/users/${user.id}`} class="mr-3 text-xs text-brand-600 hover:underline">
                  Edit
                </a>
                <button
                  type="button"
                  onclick={() => handleDelete(user)}
                  class="text-xs text-red-600 hover:text-red-800"
                >
                  Delete
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<!-- Create modal -->
{#if createOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40" onclick={() => (createOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">New user</h2>
      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div>
          <label for="nu_username" class="block text-sm font-medium text-slate-700">Username *</label>
          <input
            id="nu_username"
            type="text"
            bind:value={newUsername}
            required
            minlength="3"
            class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>
        <div>
          <label for="nu_password" class="block text-sm font-medium text-slate-700">Password *</label>
          <input
            id="nu_password"
            type="password"
            bind:value={newPassword}
            required
            minlength="8"
            class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>
        <div>
          <label for="nu_email" class="block text-sm font-medium text-slate-700">Email</label>
          <input
            id="nu_email"
            type="email"
            bind:value={newEmail}
            class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>
        <div>
          <label for="nu_full_name" class="block text-sm font-medium text-slate-700">Full name</label>
          <input
            id="nu_full_name"
            type="text"
            bind:value={newFullName}
            class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
          />
        </div>
        {#if groups.length > 0}
          <div>
            <label for="nu_group" class="block text-sm font-medium text-slate-700">Group</label>
            <select
              id="nu_group"
              bind:value={newGroupId}
              class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"
            >
              <option value={null}>— none —</option>
              {#each groups as g}
                <option value={g.id}>{g.name}</option>
              {/each}
            </select>
          </div>
        {/if}
        {#if roles.length > 0}
          <div>
            <span class="block text-sm font-medium text-slate-700">Roles</span>
            <div class="mt-1 space-y-1">
              {#each roles as r}
                <label class="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    value={r.id}
                    checked={newRoleIds.includes(r.id)}
                    onchange={(e) => {
                      if (e.currentTarget.checked) newRoleIds = [...newRoleIds, r.id]
                      else newRoleIds = newRoleIds.filter((id) => id !== r.id)
                    }}
                  />
                  <span>{r.name}</span>
                  {#if r.is_system}
                    <span class="text-xs text-slate-400">system</span>
                  {/if}
                </label>
              {/each}
            </div>
            <p class="mt-1 text-xs text-slate-500">If empty — default "manager" role will be assigned.</p>
          </div>
        {/if}
        <div class="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onclick={() => (createOpen = false)}
            class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={createBusy}
            class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300"
          >
            {createBusy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}
