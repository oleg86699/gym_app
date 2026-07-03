<script lang="ts">
  import { ArrowLeft } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import TagAccessPicker from '$lib/components/TagAccessPicker.svelte'

  import {
    groups as groupsApi,
    projects as projectsApi,
    users as usersApi,
    wpSites as wpSitesApi,
  } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { Group, Project, User } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let groupId = $derived(Number(page.params.id))

  let group = $state<Group | null>(null)
  let members = $state<User[]>([])
  let projectsList = $state<Project[]>([])
  let loading = $state(true)

  // ─── tag-access RBAC (super_admin only) ─────────────────────────────
  // Потолок разрешённых команде тегов. null = все теги; [] = ни одного;
  // [..] = только выбранные. Внутри команды group_admin раздаёт юзерам теги
  // ⊆ этого набора.
  let isSuper = $derived($currentUser?.is_super_admin ?? false)
  let availableTags = $state<string[]>([])
  let tagInfo = $state<Record<string, string>>({})  // тег → «N сайт.»
  let f_tags_restricted = $state(false)   // bind → TagAccessPicker
  let f_allowed_tags = $state<string[]>([])
  let savingTags = $state(false)
  function allowedTagsEqual(a: string[] | null, b: string[] | null): boolean {
    if (a === null || b === null) return a === b
    if (a.length !== b.length) return false
    const sa = [...a].sort()
    const sb = [...b].sort()
    return sa.every((v, i) => v === sb[i])
  }
  let tagsDirty = $derived(
    !!group && !allowedTagsEqual(f_tags_restricted ? f_allowed_tags : null, group.allowed_tags),
  )

  async function saveTags() {
    if (!groupId || !group) return
    savingTags = true
    try {
      await groupsApi.update(groupId, {
        allowed_tags: f_tags_restricted ? f_allowed_tags : null,
      })
      showToast('success', 'Теги команды обновлены')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      savingTags = false
    }
  }

  // Add members modal
  let addMembersOpen = $state(false)
  let allUsersNotInGroup = $state<User[]>([])
  let selectedAddIds = $state<number[]>([])

  async function refresh() {
    if (!groupId) return
    loading = true
    try {
      const [g, m, p] = await Promise.all([
        groupsApi.get(groupId),
        groupsApi.members(groupId),
        groupsApi.projects(groupId),
      ])
      group = g
      members = m
      projectsList = p
      // tag-access: null → все теги; массив → allowlist включён
      f_tags_restricted = g.allowed_tags !== null
      f_allowed_tags = g.allowed_tags ?? []
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  onMount(async () => {
    await refresh()
    // Теги грузим только super_admin (единственный, кто видит эту секцию).
    if ($currentUser?.is_super_admin) {
      try {
        const stats = await wpSitesApi.credentialTagsStats()
        availableTags = stats.map((s) => s.tag)
        tagInfo = Object.fromEntries(stats.map((s) => [s.tag, `${s.sites} сайт.`]))
      } catch {
        availableTags = []
        tagInfo = {}
      }
    }
  })

  async function openAddMembers() {
    try {
      const r = await usersApi.list({ limit: 200 })
      // фильтр: только юзеры НЕ в этой группе
      allUsersNotInGroup = r.items.filter((u) => u.group?.id !== groupId)
      selectedAddIds = []
      addMembersOpen = true
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function saveAddMembers() {
    if (!groupId) return
    try {
      await Promise.all(selectedAddIds.map((uid) => usersApi.update(uid, { group_id: groupId })))
      showToast('success', `Added ${selectedAddIds.length} member(s)`)
      addMembersOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function removeMember(u: User) {
    if (!confirm(`Remove @${u.username} from group "${group?.name}"?`)) return
    try {
      await usersApi.update(u.id, { is_remove_from_group: true })
      showToast('success', `@${u.username} removed`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // Share project with this group
  let shareProjectsOpen = $state(false)
  let allProjects = $state<Project[]>([])
  let selectedProjectIds = $state<number[]>([])

  async function openShareProjects() {
    try {
      const r = await projectsApi.list({ limit: 200 })
      allProjects = r.items
      selectedProjectIds = projectsList
        .filter((p) => p.shared_with_groups.some((g) => g.id === groupId))
        .map((p) => p.id)
      shareProjectsOpen = true
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function saveShareProjects() {
    if (!groupId) return
    try {
      // Для каждого изменения нужно обновить shared_with_groups проекта.
      // Делаем поштучно — для прототипа сойдёт.
      await Promise.all(
        allProjects.map(async (p) => {
          const has = p.shared_with_groups.some((g) => g.id === groupId)
          const wants = selectedProjectIds.includes(p.id)
          if (has === wants) return
          const newGroupIds = wants
            ? [...p.shared_with_groups.map((g) => g.id), groupId]
            : p.shared_with_groups.map((g) => g.id).filter((id) => id !== groupId)
          await projectsApi.shareWithGroups(p.id, newGroupIds)
        }),
      )
      showToast('success', 'Project sharing updated')
      shareProjectsOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
</script>

<div class="space-y-6">
  <div>
    <a href="/groups" class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Groups</a>
    <h1 class="mt-1 text-2xl font-semibold text-slate-900">
      {group?.name ?? '…'}
    </h1>
    {#if group?.description}
      <p class="mt-1 text-sm text-slate-500">{group.description}</p>
    {/if}
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else}
    <!-- Members -->
    <section>
      <div class="mb-2 flex items-center justify-between">
        <h2 class="text-lg font-medium text-slate-900">Members ({members.length})</h2>
        <button onclick={openAddMembers}
                class="rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium text-slate-700 hover:bg-slate-50">
          + Add members
        </button>
      </div>
      {#if members.length === 0}
        <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">
          No members yet
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-4 py-2">Username</th>
                <th class="px-4 py-2">Email</th>
                <th class="px-4 py-2">Roles</th>
                <th class="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each members as u (u.id)}
                <tr>
                  <td class="px-4 py-2 font-medium text-slate-900">@{u.username}</td>
                  <td class="px-4 py-2 text-slate-600">{u.email ?? '—'}</td>
                  <td class="px-4 py-2 text-slate-600">{u.roles.map((r) => r.name).join(', ') || '—'}</td>
                  <td class="px-4 py-2 text-right">
                    <button onclick={() => removeMember(u)} class="text-xs text-red-600 hover:text-red-800">
                      Remove
                    </button>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Projects -->
    <section>
      <div class="mb-2 flex items-center justify-between">
        <h2 class="text-lg font-medium text-slate-900">Projects ({projectsList.length})</h2>
        <button onclick={openShareProjects}
                class="rounded-md border border-slate-300 bg-white px-3 py-1 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Manage shared projects
        </button>
      </div>
      <p class="mb-2 text-xs text-slate-500">
        Все проекты, доступные группе: владельцы из группы или явно расшаренные.
      </p>
      {#if projectsList.length === 0}
        <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">
          No projects accessible to this group
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-4 py-2">Name</th>
                <th class="px-4 py-2">Owner</th>
                <th class="px-4 py-2">Type</th>
                <th class="px-4 py-2">Status</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each projectsList as p (p.id)}
                {@const ownedByGroup = p.owner_group?.id === groupId}
                <tr>
                  <td class="px-4 py-2 font-medium text-slate-900">{p.name}</td>
                  <td class="px-4 py-2 text-slate-600">@{p.owner.username}</td>
                  <td class="px-4 py-2">
                    {#if ownedByGroup}
                      <span class="rounded bg-brand-50 px-1.5 py-0.5 text-xs text-brand-700">owned</span>
                    {:else}
                      <span class="rounded bg-amber-50 px-1.5 py-0.5 text-xs text-amber-700">shared</span>
                    {/if}
                  </td>
                  <td class="px-4 py-2">
                    <span
                      class="rounded-full px-2 py-0.5 text-xs font-medium"
                      class:bg-emerald-100={p.is_active}
                      class:text-emerald-700={p.is_active}
                      class:bg-slate-200={!p.is_active}
                      class:text-slate-600={!p.is_active}
                    >
                      {p.is_active ? 'active' : 'inactive'}
                    </span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Tag access (RBAC) — super_admin only -->
    {#if isSuper}
      <section>
        <div class="mb-2 flex items-center justify-between">
          <h2 class="text-lg font-medium text-slate-900">Доступ по тегам батчей</h2>
          <button onclick={saveTags} disabled={!tagsDirty || savingTags}
                  class="rounded-md border border-brand-300 bg-brand-50 px-3 py-1 text-sm font-medium text-brand-700 hover:bg-brand-100 disabled:opacity-40">
            {savingTags ? 'Сохранение…' : 'Сохранить теги'}
          </button>
        </div>
        <div class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <p class="text-xs text-slate-500">
            Потолок разрешённых команде тегов (батчей сайтов). По умолчанию — все теги.
            group_admin внутри команды может раздавать своим юзерам только теги из этого набора.
          </p>

          <TagAccessPicker availableTags={availableTags} tagInfo={tagInfo}
                           bind:restricted={f_tags_restricted}
                           bind:selected={f_allowed_tags} />
        </div>
      </section>
    {/if}
  {/if}
</div>

<!-- Add members modal -->
{#if addMembersOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (addMembersOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-md overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Add members to "{group?.name}"</h2>
      <p class="mt-1 text-xs text-slate-500">Юзеры, которые сейчас не в этой группе.</p>

      <div class="mt-3 max-h-72 space-y-1 overflow-auto rounded border border-slate-200 p-2">
        {#each allUsersNotInGroup as u}
          <label class="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              value={u.id}
              checked={selectedAddIds.includes(u.id)}
              onchange={(e) => {
                if (e.currentTarget.checked) selectedAddIds = [...selectedAddIds, u.id]
                else selectedAddIds = selectedAddIds.filter((id) => id !== u.id)
              }}
            />
            <span>@{u.username}</span>
            {#if u.group}
              <span class="text-xs text-slate-400">currently in #{u.group.name}</span>
            {/if}
          </label>
        {/each}
        {#if allUsersNotInGroup.length === 0}
          <div class="text-xs text-slate-400">All users already in this group</div>
        {/if}
      </div>

      <div class="mt-4 flex justify-end gap-2">
        <button type="button" onclick={() => (addMembersOpen = false)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Cancel
        </button>
        <button type="button" onclick={saveAddMembers} disabled={selectedAddIds.length === 0}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
          Add {selectedAddIds.length}
        </button>
      </div>
    </div>
  </div>
{/if}

<!-- Share projects modal -->
{#if shareProjectsOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (shareProjectsOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-md overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Share projects with "{group?.name}"</h2>
      <p class="mt-1 text-xs text-slate-500">
        Отметь проекты, которые должны быть доступны всей группе.
        Проекты с владельцем из группы скрыты — у них уже есть доступ.
      </p>

      <div class="mt-3 max-h-72 space-y-1 overflow-auto rounded border border-slate-200 p-2">
        {#each allProjects.filter((p) => p.owner_group?.id !== groupId) as p}
          <label class="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              value={p.id}
              checked={selectedProjectIds.includes(p.id)}
              onchange={(e) => {
                if (e.currentTarget.checked) selectedProjectIds = [...selectedProjectIds, p.id]
                else selectedProjectIds = selectedProjectIds.filter((id) => id !== p.id)
              }}
            />
            <span>{p.name}</span>
            <span class="text-xs text-slate-400">@{p.owner.username}</span>
          </label>
        {/each}
      </div>

      <div class="mt-4 flex justify-end gap-2">
        <button type="button" onclick={() => (shareProjectsOpen = false)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Cancel
        </button>
        <button type="button" onclick={saveShareProjects}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">
          Save
        </button>
      </div>
    </div>
  </div>
{/if}
