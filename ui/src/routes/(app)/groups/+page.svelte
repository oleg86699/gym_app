<script lang="ts">
  import { Pencil, Power, X } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import {
    groups as groupsApi,
    projects as projectsApi,
    wpSites as wpSitesApi,
  } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { GroupListItem, Project } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import TagAccessPicker from '$lib/components/TagAccessPicker.svelte'

  let items = $state<GroupListItem[]>([])
  let allProjects = $state<Project[]>([])
  let allTags = $state<string[]>([])
  let loading = $state(true)

  // Create
  let createOpen = $state(false)
  let newName = $state('')
  let newDescription = $state('')

  // Edit
  let editing = $state<GroupListItem | null>(null)
  let editName = $state('')
  let editDescription = $state('')
  let editIsActive = $state(true)
  let editSharedProjectIds = $state<number[]>([])
  let editBusy = $state(false)

  // ─── tag-access RBAC (потолок команды; /groups — super_admin only) ──
  // bind → TagAccessPicker
  let editTagsRestricted = $state(false)
  let editAllowedTags = $state<string[]>([])

  async function refresh() {
    loading = true
    try {
      const [g, p] = await Promise.all([
        groupsApi.list(),
        projectsApi.list({ limit: 200 }).catch(() => ({ items: [] as Project[], next_cursor: null, has_more: false })),
      ])
      items = g
      allProjects = p.items
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
    // Теги — отдельным запросом, чтобы ошибка не роняла список групп.
    try {
      allTags = await wpSitesApi.credentialTags()
    } catch {
      allTags = []
    }
  }
  onMount(refresh)

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    try {
      await groupsApi.create({ name: newName, description: newDescription || undefined })
      showToast('success', `Group "${newName}" created`)
      createOpen = false
      newName = ''
      newDescription = ''
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function openEdit(g: GroupListItem) {
    editing = g
    editName = g.name
    editDescription = g.description ?? ''
    editIsActive = g.is_active
    editSharedProjectIds = g.shared_projects.map((p) => p.id)
    // tag-access: null → все теги; массив → allowlist включён
    editTagsRestricted = g.allowed_tags !== null
    editAllowedTags = g.allowed_tags ?? []
  }

  async function saveEdit() {
    if (!editing) return
    editBusy = true
    try {
      await groupsApi.update(editing.id, {
        name: editName !== editing.name ? editName : undefined,
        description: editDescription !== (editing.description ?? '') ? editDescription : undefined,
        is_active: editIsActive !== editing.is_active ? editIsActive : undefined,
        shared_project_ids: editSharedProjectIds,
        // null = снять ограничение (все теги); массив = allowlist
        allowed_tags: editTagsRestricted ? editAllowedTags : null,
      })
      showToast('success', `Group "${editName}" updated`)
      editing = null
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      editBusy = false
    }
  }

  async function toggleActive(g: GroupListItem) {
    try {
      await groupsApi.update(g.id, { is_active: !g.is_active })
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function handleDelete(g: GroupListItem) {
    if (!confirm(`Delete group "${g.name}"?`)) return
    try {
      await groupsApi.remove(g.id)
      showToast('success', 'Group deleted')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
</script>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Groups</h1>
      <p class="mt-1 text-sm text-slate-500">
        {items.length} team(s). Click name for detail view.
      </p>
    </div>
    <button
      type="button"
      onclick={() => (createOpen = true)}
      class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700"
    >
      + New group
    </button>
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">No groups</div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-4 py-2">ID</th>
            <th class="px-4 py-2">Group name</th>
            <th class="px-4 py-2">Description</th>
            <th class="px-4 py-2">Projects</th>
            <th class="px-4 py-2 text-center">Users</th>
            <th class="px-4 py-2 text-center">Status</th>
            <th class="px-4 py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as g (g.id)}
            <tr>
              <td class="px-4 py-3 text-slate-500">{g.id}</td>
              <td class="px-4 py-3 font-medium text-slate-900">
                <a href={`/groups/${g.id}`} class="text-brand-600 hover:underline">{g.name}</a>
              </td>
              <td class="px-4 py-3 text-slate-600">{g.description ?? '—'}</td>
              <td class="max-w-md px-4 py-3">
                {#if g.owned_projects.length + g.shared_projects.length === 0}
                  <span class="text-xs text-slate-400">—</span>
                {:else}
                  <div class="flex flex-wrap gap-1">
                    {#each g.owned_projects as p}
                      <span class="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] text-brand-700" title="owned by group member">
                        {p.name}
                      </span>
                    {/each}
                    {#each g.shared_projects as p}
                      <span class="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] text-amber-700" title="explicitly shared with group">
                        {p.name}
                      </span>
                    {/each}
                  </div>
                {/if}
              </td>
              <td class="px-4 py-3 text-center text-slate-600">{g.members_count}</td>
              <td class="px-4 py-3 text-center">
                {#if g.is_active}
                  <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-medium uppercase text-emerald-700">active</span>
                {:else}
                  <span class="rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-medium uppercase text-slate-600">off</span>
                {/if}
              </td>
              <td class="px-4 py-3 text-right whitespace-nowrap">
                <button onclick={() => openEdit(g)} title="Edit"
                        class="inline-flex h-8 w-8 items-center justify-center rounded-md bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
                  <Pencil size={14} class="inline-block align-text-bottom" />
                </button>
                <button onclick={() => toggleActive(g)} title={g.is_active ? 'Deactivate' : 'Activate'}
                        class="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-md bg-brand-50 text-brand-700 hover:bg-brand-100">
                  <Power size={14} class="inline-block align-text-bottom" />
                </button>
                <button onclick={() => handleDelete(g)} title="Delete"
                        class="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-md bg-red-50 text-red-700 hover:bg-red-100">
                  <X size={14} class="inline-block align-text-bottom" />
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
      <h2 class="text-lg font-semibold text-slate-900">New group</h2>
      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div>
          <label for="g_name" class="block text-sm font-medium text-slate-700">Name *</label>
          <input id="g_name" type="text" bind:value={newName} required
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="g_desc" class="block text-sm font-medium text-slate-700">Description</label>
          <textarea id="g_desc" bind:value={newDescription} rows="2"
                    class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"></textarea>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Cancel
          </button>
          <button type="submit" class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">
            Create
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Edit modal: name, description, status, project assignment -->
{#if editing}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (editing = null)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Edit group: {editing.name}</h2>

      <div class="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <label for="eg_name" class="block text-sm font-medium text-slate-700">Name</label>
          <input id="eg_name" type="text" bind:value={editName}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="eg_status" class="block text-sm font-medium text-slate-700">Status</label>
          <select id="eg_status" bind:value={editIsActive}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            <option value={true}>Active</option>
            <option value={false}>Inactive</option>
          </select>
        </div>
      </div>
      <div class="mt-3">
        <label for="eg_desc" class="block text-sm font-medium text-slate-700">Description</label>
        <textarea id="eg_desc" bind:value={editDescription} rows="2"
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"></textarea>
      </div>

      <h3 class="mt-6 text-sm font-medium text-slate-700">Project access (доступ для группы)</h3>
      <p class="text-xs text-slate-500">
        Отметь проекты → выдать доступ всей группе.
        <span class="font-medium text-indigo-600">фиолетовый</span> — принадлежит группе (владелец из группы, доступ автоматический);
        <span class="font-medium text-emerald-600">зелёный</span> — доступ выдан группе (shared);
        серый — доступа нет. Owned — read-only (галка заблокирована).
      </p>
      <div class="mt-2 grid max-h-72 gap-2 overflow-auto rounded border border-slate-200 p-2 sm:grid-cols-2">
        {#each allProjects as p}
          {@const ownedByGroup = p.owner_group?.id === editing?.id}
          {@const shared = !ownedByGroup && editSharedProjectIds.includes(p.id)}
          <label class="flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                 class:bg-indigo-50={ownedByGroup}
                 class:border-indigo-200={ownedByGroup}
                 class:bg-emerald-50={shared}
                 class:border-emerald-200={shared}
                 class:bg-slate-50={!ownedByGroup && !shared}
                 class:border-slate-200={!ownedByGroup && !shared}>
            <input
              type="checkbox"
              value={p.id}
              checked={ownedByGroup || editSharedProjectIds.includes(p.id)}
              disabled={ownedByGroup}
              onchange={(e) => {
                if (e.currentTarget.checked) editSharedProjectIds = [...editSharedProjectIds, p.id]
                else editSharedProjectIds = editSharedProjectIds.filter((id) => id !== p.id)
              }} />
            <div class="flex-1">
              <div class="flex flex-wrap items-center gap-1.5">
                <span class="font-medium text-slate-900">{p.name}</span>
                {#if ownedByGroup}
                  <span class="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-indigo-700">владелец</span>
                {:else if shared}
                  <span class="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-emerald-700">shared</span>
                {/if}
              </div>
              <div class="text-xs text-slate-400">@{p.owner.username}</div>
            </div>
          </label>
        {/each}
        {#if allProjects.length === 0}
          <div class="text-xs text-slate-400">No projects yet</div>
        {/if}
      </div>

      <h3 class="mt-6 text-sm font-medium text-slate-700">Доступ по тегам батчей</h3>
      <p class="text-xs text-slate-500">
        Потолок разрешённых команде тегов (батчей сайтов). По умолчанию — все теги.
        group_admin внутри команды раздаёт своим юзерам только теги из этого набора.
      </p>
      <TagAccessPicker availableTags={allTags}
                       bind:restricted={editTagsRestricted}
                       bind:selected={editAllowedTags} />

      <div class="mt-6 flex justify-end gap-2">
        <button type="button" onclick={() => (editing = null)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Cancel
        </button>
        <button type="button" onclick={saveEdit} disabled={editBusy}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
          {editBusy ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </div>
  </div>
{/if}
