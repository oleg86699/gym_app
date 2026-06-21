<script lang="ts">
  import { Pencil, Power, X } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { pages as pagesApi, permissions as permsApi, roles as rolesApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { AdminPage, Permission, Role } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let items = $state<Role[]>([])
  let allPerms = $state<Permission[]>([])
  let allPages = $state<AdminPage[]>([])
  let loading = $state(true)

  let search = $state('')
  let rowsPerPage = $state(10)

  let isSuper = $derived($currentUser?.is_super_admin ?? false)

  let filtered = $derived(
    items.filter((r) => {
      if (!search.trim()) return true
      const q = search.trim().toLowerCase()
      return r.name.toLowerCase().includes(q) || (r.description ?? '').toLowerCase().includes(q)
    }),
  )
  let visible = $derived(filtered.slice(0, rowsPerPage))

  // Edit modal
  let editing = $state<Role | null>(null)
  let editPermIds = $state<number[]>([])
  let editPageIds = $state<number[]>([])
  let editDelegate = $state(false)

  // Create modal
  let createOpen = $state(false)
  let createBusy = $state(false)
  let newName = $state('')
  let newDescription = $state('')
  let newPermIds = $state<number[]>([])
  let newPageIds = $state<number[]>([])
  let newDelegate = $state(false)

  async function refresh() {
    loading = true
    try {
      const [r, p, pg] = await Promise.all([
        rolesApi.list(),
        permsApi.list().catch(() => [] as Permission[]),
        pagesApi.list().catch(() => [] as AdminPage[]),
      ])
      items = r
      allPerms = p
      allPages = pg
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(refresh)

  function openEdit(role: Role) {
    editing = role
    editPermIds = role.permissions.map((p) => p.id)
    editPageIds = role.pages.map((p) => p.id)
    editDelegate = role.is_assignable_by_group_admin
  }

  async function saveEdit() {
    if (!editing) return
    try {
      await rolesApi.update(editing.id, {
        permission_ids: editPermIds,
        page_ids: editPageIds,
        is_assignable_by_group_admin: editDelegate,
      })
      showToast('success', `Role "${editing.name}" updated`)
      editing = null
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    createBusy = true
    try {
      await rolesApi.create({
        name: newName,
        description: newDescription || undefined,
        permission_ids: newPermIds.length ? newPermIds : undefined,
        page_ids: newPageIds.length ? newPageIds : undefined,
        is_assignable_by_group_admin: newDelegate,
      })
      showToast('success', `Role "${newName}" created`)
      createOpen = false
      newName = ''
      newDescription = ''
      newPermIds = []
      newPageIds = []
      newDelegate = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      createBusy = false
    }
  }

  async function handleDelete(role: Role) {
    if (role.is_system) return
    if (!confirm(`Delete role "${role.name}"?`)) return
    try {
      await rolesApi.remove(role.id)
      showToast('success', 'Role deleted')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function toggleActive(role: Role) {
    if (role.name === 'super_admin') return
    try {
      await rolesApi.update(role.id, { is_active: !role.is_active })
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
</script>

<div class="space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Roles</h1>
    <p class="mt-1 text-sm text-slate-500">Role setup with permissions and page-level access.</p>
  </div>

  <div class="flex flex-wrap items-end gap-4">
    <div class="min-w-[240px] flex-1">
      <label for="role_search" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Search</label>
      <input id="role_search" type="search" bind:value={search}
             placeholder="Search by role name or description"
             class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
    </div>
    <div>
      <label for="rows_pp" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Rows</label>
      <select id="rows_pp" bind:value={rowsPerPage}
              class="mt-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm">
        <option value={10}>10</option>
        <option value={25}>25</option>
        <option value={50}>50</option>
        <option value={100}>100</option>
      </select>
    </div>
    {#if isSuper}
      <button type="button" onclick={() => (createOpen = true)}
              class="ml-auto rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
        + Add role
      </button>
    {/if}
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if visible.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      No roles match
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-4 py-2">ID</th>
            <th class="px-4 py-2">Role</th>
            <th class="px-4 py-2">Description</th>
            <th class="px-4 py-2">Permissions</th>
            <th class="px-4 py-2">Pages</th>
            <th class="px-4 py-2 text-center">Delegate</th>
            <th class="px-4 py-2 text-center">Status</th>
            <th class="px-4 py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each visible as r (r.id)}
            <tr>
              <td class="px-4 py-3 text-slate-500">{r.id}</td>
              <td class="px-4 py-3">
                <div class="font-medium text-slate-900">{r.name}</div>
                {#if r.is_system}
                  <span class="text-xs text-slate-400">system</span>
                {/if}
              </td>
              <td class="px-4 py-3 text-slate-600">{r.description ?? '—'}</td>
              <td class="max-w-xs px-4 py-3">
                {#if r.permissions.length === 0}
                  <span class="text-xs text-slate-400">—</span>
                {:else}
                  <div class="flex flex-wrap gap-1">
                    {#each r.permissions as p}
                      <span class="rounded-full bg-emerald-50 px-2 py-0.5 font-mono text-[11px] text-emerald-700">
                        {p.code}
                      </span>
                    {/each}
                  </div>
                {/if}
              </td>
              <td class="max-w-xs px-4 py-3">
                {#if r.pages.length === 0}
                  <span class="text-xs text-slate-400">—</span>
                {:else}
                  <div class="flex flex-wrap gap-1">
                    {#each r.pages as pg}
                      <span class="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] text-brand-700">{pg.name}</span>
                    {/each}
                  </div>
                {/if}
              </td>
              <td class="px-4 py-3 text-center">
                {#if r.is_assignable_by_group_admin}
                  <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium text-emerald-700">yes</span>
                {:else}
                  <span class="text-xs text-slate-300">—</span>
                {/if}
              </td>
              <td class="px-4 py-3 text-center">
                {#if r.is_active}
                  <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium uppercase text-emerald-700">active</span>
                {:else}
                  <span class="rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-medium uppercase text-slate-600">off</span>
                {/if}
              </td>
              <td class="px-4 py-3 text-right whitespace-nowrap">
                {#if isSuper}
                  {#if r.name !== 'super_admin'}
                    <button onclick={() => openEdit(r)} title="Edit"
                            class="inline-flex h-8 w-8 items-center justify-center rounded-md bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
                      <Pencil size={14} class="inline-block align-text-bottom" />
                    </button>
                    <button onclick={() => toggleActive(r)} title={r.is_active ? 'Deactivate' : 'Activate'}
                            class="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-md bg-brand-50 text-brand-700 hover:bg-brand-100">
                      <Power size={14} class="inline-block align-text-bottom" />
                    </button>
                  {/if}
                  {#if !r.is_system}
                    <button onclick={() => handleDelete(r)} title="Delete"
                            class="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-md bg-red-50 text-red-700 hover:bg-red-100">
                      <X size={14} class="inline-block align-text-bottom" />
                    </button>
                  {/if}
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if filtered.length > rowsPerPage}
      <p class="text-xs text-slate-500">
        Showing {visible.length} of {filtered.length}. Increase «Rows» to see more.
      </p>
    {/if}
  {/if}
</div>

<!-- Edit modal -->
{#if editing}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (editing = null)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Edit role: {editing.name}</h2>
      <div class="mt-4">
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" bind:checked={editDelegate} />
          <span>Делегировать group_admin</span>
        </label>
      </div>
      <div class="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <h3 class="text-sm font-medium text-slate-700">Permissions</h3>
          <div class="mt-2 max-h-80 space-y-1 overflow-auto rounded border border-slate-200 p-2">
            {#each allPerms as p}
              <label class="flex items-center gap-2 text-sm">
                <input type="checkbox" value={p.id} checked={editPermIds.includes(p.id)}
                       onchange={(e) => {
                         if (e.currentTarget.checked) editPermIds = [...editPermIds, p.id]
                         else editPermIds = editPermIds.filter((id) => id !== p.id)
                       }} />
                <span class="font-mono text-xs">{p.code}</span>
              </label>
            {/each}
          </div>
        </div>
        <div>
          <h3 class="text-sm font-medium text-slate-700">Pages</h3>
          <div class="mt-2 max-h-80 space-y-1 overflow-auto rounded border border-slate-200 p-2">
            {#each allPages as pg}
              <label class="flex items-center gap-2 text-sm">
                <input type="checkbox" value={pg.id} checked={editPageIds.includes(pg.id)}
                       onchange={(e) => {
                         if (e.currentTarget.checked) editPageIds = [...editPageIds, pg.id]
                         else editPageIds = editPageIds.filter((id) => id !== pg.id)
                       }} />
                <span class="font-mono text-xs">{pg.path}</span>
              </label>
            {/each}
          </div>
        </div>
      </div>
      <div class="mt-6 flex justify-end gap-2">
        <button type="button" onclick={() => (editing = null)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Cancel
        </button>
        <button type="button" onclick={saveEdit}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">
          Save
        </button>
      </div>
    </div>
  </div>
{/if}

<!-- Create modal -->
{#if createOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">New role</h2>
      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div>
          <label for="r_name" class="block text-sm font-medium text-slate-700">Name *</label>
          <input id="r_name" type="text" bind:value={newName} required minlength="1"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="r_desc" class="block text-sm font-medium text-slate-700">Description</label>
          <textarea id="r_desc" bind:value={newDescription} rows="2"
                    class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"></textarea>
        </div>
        <div>
          <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={newDelegate} />
            <span>Делегировать group_admin</span>
          </label>
        </div>
        <div class="grid gap-4 sm:grid-cols-2">
          <div>
            <h3 class="text-sm font-medium text-slate-700">Permissions</h3>
            <div class="mt-2 max-h-64 space-y-1 overflow-auto rounded border border-slate-200 p-2">
              {#each allPerms as p}
                <label class="flex items-center gap-2 text-sm">
                  <input type="checkbox" value={p.id} checked={newPermIds.includes(p.id)}
                         onchange={(e) => {
                           if (e.currentTarget.checked) newPermIds = [...newPermIds, p.id]
                           else newPermIds = newPermIds.filter((id) => id !== p.id)
                         }} />
                  <span class="font-mono text-xs">{p.code}</span>
                </label>
              {/each}
            </div>
          </div>
          <div>
            <h3 class="text-sm font-medium text-slate-700">Pages</h3>
            <div class="mt-2 max-h-64 space-y-1 overflow-auto rounded border border-slate-200 p-2">
              {#each allPages as pg}
                <label class="flex items-center gap-2 text-sm">
                  <input type="checkbox" value={pg.id} checked={newPageIds.includes(pg.id)}
                         onchange={(e) => {
                           if (e.currentTarget.checked) newPageIds = [...newPageIds, pg.id]
                           else newPageIds = newPageIds.filter((id) => id !== pg.id)
                         }} />
                  <span class="font-mono text-xs">{pg.path}</span>
                </label>
              {/each}
            </div>
          </div>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Cancel
          </button>
          <button type="submit" disabled={createBusy}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {createBusy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}
