<script lang="ts">
  import { Activity, AlertTriangle } from 'lucide-svelte'
  import { onDestroy, onMount } from 'svelte'

  import { groups as groupsApi, projects as projectsApi, users as usersApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { GroupListItem, Project, ProjectListItem, User } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser, hasPermission } from '$lib/stores/user'

  let items = $state<ProjectListItem[]>([])
  let allGroups = $state<GroupListItem[]>([])
  let allUsers = $state<User[]>([])
  let loading = $state(true)
  let search = $state('')

  // Может ли текущий юзер шарить проекты вообще
  let canShareAny = $derived(hasPermission($currentUser, 'projects.share'))

  let createOpen = $state(false)
  let newName = $state('')
  let newDescription = $state('')

  let shareOpen = $state<Project | null>(null)
  let shareUserIds = $state<number[]>([])
  let shareGroupIds = $state<number[]>([])
  let shareBusy = $state(false)

  async function refresh() {
    loading = true
    try {
      const r = await projectsApi.list({ search, limit: 100 })
      items = r.items
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadAux() {
    // Юзеры нужны всем кто может шарить (включая обычного owner-а — он шарит со своей командой)
    try {
      const u = await usersApi.list({ limit: 200 })
      allUsers = u.items
    } catch {
      allUsers = []
    }
    // Группы нужны только при наличии projects.share — иначе share с группами недоступен
    if (canShareAny) {
      try {
        allGroups = await groupsApi.list()
      } catch {
        allGroups = []
      }
    }
  }

  // Poll каждые 5 сек если в любом из проектов есть active runs
  let pollTimer: ReturnType<typeof setInterval> | null = null
  function hasActiveAnywhere(): boolean {
    return items.some((p) => p.active_runs > 0)
  }

  onMount(async () => {
    await Promise.all([refresh(), loadAux()])
    pollTimer = setInterval(() => {
      if (hasActiveAnywhere()) refresh()
    }, 5000)
  })
  onDestroy(() => { if (pollTimer) clearInterval(pollTimer) })

  function relTime(iso: string | null): string {
    if (!iso) return 'never'
    const diff = Date.now() - new Date(iso).getTime()
    if (diff < 0) return 'in future'
    const s = Math.floor(diff / 1000)
    if (s < 60) return `${s}s ago`
    const m = Math.floor(s / 60)
    if (m < 60) return `${m}m ago`
    const h = Math.floor(m / 60)
    if (h < 48) return `${h}h ago`
    const d = Math.floor(h / 24)
    if (d < 30) return `${d}d ago`
    const mo = Math.floor(d / 30)
    return mo < 12 ? `${mo}mo ago` : `${Math.floor(mo / 12)}y ago`
  }
  function fullTs(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : ''
  }

  async function handleSearch(e: SubmitEvent) {
    e.preventDefault()
    await refresh()
  }

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    try {
      await projectsApi.create({
        name: newName,
        description: newDescription || undefined,
      })
      showToast('success', `Project "${newName}" created`)
      createOpen = false
      newName = ''
      newDescription = ''
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function handleDelete(p: Project) {
    if (!confirm(`Delete project "${p.name}"?`)) return
    try {
      await projectsApi.remove(p.id)
      showToast('success', 'Project deleted')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function openShare(p: Project) {
    shareOpen = p
    shareUserIds = p.shared_with_users.map((u) => u.id)
    shareGroupIds = p.shared_with_groups.map((g) => g.id)
  }

  async function saveShare() {
    if (!shareOpen) return
    shareBusy = true
    try {
      await projectsApi.shareWithUsers(shareOpen.id, shareUserIds)
      // С группами шарит только тот, у кого projects.share permission
      if (canShareAny) {
        await projectsApi.shareWithGroups(shareOpen.id, shareGroupIds)
      }
      showToast('success', 'Sharing updated')
      shareOpen = null
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      shareBusy = false
    }
  }

  function canManage(p: Project): boolean {
    const u = $currentUser
    if (!u) return false
    if (u.is_super_admin) return true
    if (p.owner.id === u.id) return true
    if (u.roles.some((r) => r.name === 'group_admin') && p.owner_group?.id === u.group?.id) return true
    return false
  }

  function canShare(p: Project): boolean {
    // Любой, кто может управлять проектом, может шарить. Scope на бэке —
    // обычный owner шарит только с своими тиммейтами; группы только при canShareAny.
    return canManage(p)
  }
</script>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Projects</h1>
      <p class="mt-1 text-sm text-slate-500">
        {items.length} project(s). Видны: свои + расшаренные с тобой/твоей группой.
      </p>
    </div>
    <button
      type="button"
      onclick={() => (createOpen = true)}
      class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700"
    >
      + New project
    </button>
  </div>

  <form onsubmit={handleSearch} class="flex gap-2">
    <input
      type="search"
      bind:value={search}
      placeholder="Search by name…"
      class="flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500"
    />
    <button class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
      Search
    </button>
  </form>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      No projects yet. Create one.
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-4 py-2">Name</th>
            <th class="px-4 py-2">Owner</th>
            <th class="px-4 py-2">Shared with</th>
            <th class="px-4 py-2 text-center" title="Сейчас выполняется: queued / running / paused / scheduled / unpacking">Active</th>
            <th class="px-4 py-2 text-center" title="Опубликовано постов (lifetime) · последние 24ч">Posted</th>
            <th class="px-4 py-2 text-center" title="Сайтов, на которые проект может постить (валидный admin + рабочий канал XML-RPC/wp-admin) и которые ещё не использованы в проекте. Совпадает с реальным пулом постинга.">Admins free</th>
            <th class="px-4 py-2 text-center" title="Runs в проблемных статусах: failed / need_more_admins / interrupted">Issues</th>
            <th class="px-4 py-2 text-center" title="Последняя активность: max(post, run created)">Last activity</th>
            <th class="px-4 py-2 text-center">Status</th>
            <th class="px-4 py-2"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as p (p.id)}
            {@const adminsPool = p.valid_admins_pool}
            {@const adminsAvail = p.available_admins}
            {@const adminsLow = adminsPool > 0 && adminsAvail < Math.max(adminsPool * 0.1, 5)}
            <tr class="hover:bg-slate-50">
              <td class="px-4 py-2 font-medium text-slate-900">
                <a href={`/projects/${p.id}`} class="text-brand-600 hover:underline">{p.name}</a>
                {#if p.owner_group}
                  <div class="text-[10px] text-slate-400">#{p.owner_group.name}</div>
                {/if}
              </td>
              <td class="px-4 py-2 text-slate-600">@{p.owner.username}</td>
              <td class="px-4 py-2 text-slate-600">
                {#if p.shared_with_users.length + p.shared_with_groups.length === 0}
                  <span class="text-slate-300">—</span>
                {:else}
                  <div class="flex flex-wrap gap-1">
                    {#each p.shared_with_users as u}
                      <span class="rounded bg-slate-100 px-1.5 py-0.5 text-xs">@{u.username}</span>
                    {/each}
                    {#each p.shared_with_groups as g}
                      <span class="rounded bg-brand-50 px-1.5 py-0.5 text-xs text-brand-700">#{g.name}</span>
                    {/each}
                  </div>
                {/if}
              </td>
              <td class="px-4 py-2 text-center">
                {#if p.active_runs > 0}
                  <span class="inline-flex items-center gap-1 rounded-full bg-brand-100 px-2 py-0.5 text-xs font-medium text-brand-700"
                        title="{p.active_runs} runs работают сейчас">
                    <span class="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-brand-600"></span>
                    <Activity size={11} class="inline" /> {p.active_runs}
                  </span>
                {:else}
                  <span class="text-xs text-slate-300">—</span>
                {/if}
              </td>
              <td class="px-4 py-2 text-center">
                {#if p.posted_total > 0}
                  <span class="text-sm font-medium text-slate-800" title="Всего опубликовано постов (text_items.status=posted)">
                    {p.posted_total}
                  </span>
                  {#if p.posted_24h > 0}
                    <div class="text-[10px] text-emerald-600" title="За последние 24 часа">
                      +{p.posted_24h} / 24h
                    </div>
                  {/if}
                {:else}
                  <span class="text-xs text-slate-300">—</span>
                {/if}
              </td>
              <td class="px-4 py-2 text-center">
                {#if adminsPool === 0}
                  <span class="text-xs text-slate-300" title="Пул постабельных сайтов пуст">—</span>
                {:else}
                  <span class="text-sm font-medium"
                        class:text-emerald-700={!adminsLow}
                        class:text-amber-700={adminsLow}
                        title="Сайтов, на которые проект ещё может постить: {adminsAvail} из {adminsPool} постабельных в пуле (валидный admin + рабочий канал). Сайт «использован» = ≥1 публикация в проекте; лимит повторов — в самой задаче.">
                    {adminsAvail}
                  </span>
                  <div class="text-[10px] text-slate-400">of {adminsPool}</div>
                {/if}
              </td>
              <td class="px-4 py-2 text-center">
                {#if p.failed_runs > 0}
                  <span class="inline-flex items-center gap-1 rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700"
                        title="{p.failed_runs} runs в failed/need_more_admins/interrupted — нужно вмешательство">
                    <AlertTriangle size={11} class="inline" /> {p.failed_runs}
                  </span>
                {:else}
                  <span class="text-xs text-slate-300">—</span>
                {/if}
              </td>
              <td class="px-4 py-2 text-center text-xs text-slate-500" title={fullTs(p.last_activity_at)}>
                {relTime(p.last_activity_at)}
                {#if p.runs_total > 0}
                  <div class="text-[10px] text-slate-400">{p.runs_total} runs total</div>
                {/if}
              </td>
              <td class="px-4 py-2 text-center">
                <span class="rounded-full px-2 py-0.5 text-xs font-medium"
                      class:bg-emerald-100={p.is_active}
                      class:text-emerald-700={p.is_active}
                      class:bg-slate-200={!p.is_active}
                      class:text-slate-600={!p.is_active}>
                  {p.is_active ? 'active' : 'inactive'}
                </span>
              </td>
              <td class="px-4 py-2 text-right whitespace-nowrap">
                {#if canShare(p)}
                  <button onclick={() => openShare(p)} class="mr-2 text-xs text-brand-600 hover:underline">
                    Share
                  </button>
                {/if}
                {#if canManage(p)}
                  <button onclick={() => handleDelete(p)} class="text-xs text-red-600 hover:text-red-800">
                    Delete
                  </button>
                {/if}
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
      <h2 class="text-lg font-semibold text-slate-900">New project</h2>
      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div>
          <label for="p_name" class="block text-sm font-medium text-slate-700">Name *</label>
          <input id="p_name" type="text" bind:value={newName} required
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="p_desc" class="block text-sm font-medium text-slate-700">Description</label>
          <textarea id="p_desc" bind:value={newDescription} rows="2"
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

<!-- Share modal -->
{#if shareOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (shareOpen = null)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Share project: {shareOpen.name}</h2>
      <p class="mt-1 text-sm text-slate-500">
        Owner и group_admin владельческой группы видят проект автоматически. Здесь — дополнительные.
      </p>

      <div class="mt-4 grid gap-4" class:sm:grid-cols-2={canShareAny}>
        <div>
          <h3 class="text-sm font-medium text-slate-700">Share with users</h3>
          {#if !canShareAny}
            <p class="mt-1 text-xs text-slate-500">Доступны только участники твоей команды.</p>
          {/if}
          <div class="mt-2 max-h-64 space-y-1 overflow-auto rounded border border-slate-200 p-2">
            {#each allUsers.filter((u) => u.id !== shareOpen?.owner.id) as u}
              <label class="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  value={u.id}
                  checked={shareUserIds.includes(u.id)}
                  onchange={(e) => {
                    if (e.currentTarget.checked) shareUserIds = [...shareUserIds, u.id]
                    else shareUserIds = shareUserIds.filter((id) => id !== u.id)
                  }}
                />
                <span>@{u.username}</span>
                {#if u.group}
                  <span class="text-xs text-slate-400">#{u.group.name}</span>
                {/if}
              </label>
            {/each}
            {#if allUsers.length === 0}
              <div class="text-xs text-slate-400">No teammates</div>
            {/if}
          </div>
        </div>

        {#if canShareAny}
          <div>
            <h3 class="text-sm font-medium text-slate-700">Share with groups</h3>
            <div class="mt-2 max-h-64 space-y-1 overflow-auto rounded border border-slate-200 p-2">
              {#each allGroups as g}
                <label class="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    value={g.id}
                    checked={shareGroupIds.includes(g.id)}
                    onchange={(e) => {
                      if (e.currentTarget.checked) shareGroupIds = [...shareGroupIds, g.id]
                      else shareGroupIds = shareGroupIds.filter((id) => id !== g.id)
                    }}
                  />
                  <span>#{g.name}</span>
                </label>
              {/each}
              {#if allGroups.length === 0}
                <div class="text-xs text-slate-400">No groups</div>
              {/if}
            </div>
          </div>
        {/if}
      </div>

      <div class="mt-6 flex justify-end gap-2">
        <button type="button" onclick={() => (shareOpen = null)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Cancel
        </button>
        <button type="button" onclick={saveShare} disabled={shareBusy}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
          {shareBusy ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  </div>
{/if}
