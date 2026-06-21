<script lang="ts">
  import { X } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { audit as auditApi, users as usersApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { AuditEntry, User } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let items = $state<AuditEntry[]>([])
  let loading = $state(true)
  let hasMore = $state(false)
  let actions = $state<string[]>([])
  let usersForFilter = $state<User[]>([])

  // Filters
  let filterAction = $state('')
  let filterActor = $state<number | null>(null)
  let filterResourceType = $state('')

  async function refresh() {
    loading = true
    try {
      const res = await auditApi.list({
        action: filterAction || undefined,
        actor_id: filterActor ?? undefined,
        resource_type: filterResourceType || undefined,
        limit: 100,
      })
      items = res.items
      hasMore = res.has_more
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadMore() {
    if (!hasMore || items.length === 0) return
    try {
      const res = await auditApi.list({
        action: filterAction || undefined,
        actor_id: filterActor ?? undefined,
        resource_type: filterResourceType || undefined,
        after_id: items[items.length - 1].id,
        limit: 100,
      })
      items = [...items, ...res.items]
      hasMore = res.has_more
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function loadMeta() {
    try {
      actions = await auditApi.actions()
    } catch {
      actions = []
    }
    try {
      const r = await usersApi.list({ limit: 200 })
      usersForFilter = r.items
    } catch {
      usersForFilter = []
    }
  }

  onMount(async () => {
    await Promise.all([refresh(), loadMeta()])
  })

  function actionColor(action: string): string {
    if (action.includes('delete') || action.includes('remove')) return 'bg-red-100 text-red-700'
    if (action.includes('create') || action.includes('import')) return 'bg-emerald-100 text-emerald-700'
    if (action.includes('update') || action.includes('pause') || action.includes('resume') || action.includes('retry')) return 'bg-amber-100 text-amber-700'
    if (action.includes('login_failed')) return 'bg-red-100 text-red-700'
    if (action.includes('login')) return 'bg-brand-100 text-brand-700'
    if (action.includes('export')) return 'bg-purple-100 text-purple-700'
    return 'bg-slate-100 text-slate-600'
  }

  function fmtChanges(c: Record<string, unknown> | null): string {
    if (!c || Object.keys(c).length === 0) return '—'
    return JSON.stringify(c)
  }

  // Уникальные resource_type для фильтра
  let resourceTypes = $derived(Array.from(new Set(items.map((i) => i.resource_type).filter(Boolean))) as string[])
</script>

<div class="space-y-4">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Audit log</h1>
    <p class="mt-1 text-sm text-slate-500">
      Append-only история ключевых действий: логины, изменения users/projects/runs/wp-sites/proxies/settings.
    </p>
  </div>

  <!-- Filters -->
  <div class="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-white p-3">
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Action
      <select bind:value={filterAction} onchange={() => refresh()}
              class="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value="">All</option>
        {#each actions as a}
          <option value={a}>{a}</option>
        {/each}
      </select>
    </label>
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Actor
      <select bind:value={filterActor} onchange={() => refresh()}
              class="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value={null}>All</option>
        {#each usersForFilter as u}
          <option value={u.id}>@{u.username}</option>
        {/each}
      </select>
    </label>
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Resource
      <input type="text" bind:value={filterResourceType} placeholder="user / run / proxy / ..."
             onchange={() => refresh()}
             class="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
    </label>
    {#if filterAction || filterActor || filterResourceType}
      <button type="button" onclick={() => { filterAction = ''; filterActor = null; filterResourceType = ''; refresh() }}
              class="ml-auto rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50">
        <X size={14} class="inline-block align-text-bottom" /> Clear
      </button>
    {/if}
  </div>

  <p class="text-xs text-slate-400">
    {#if loading}Loading…{:else}Showing {items.length} entries{#if hasMore} (more available — scroll/Load more){/if}{/if}
  </p>

  <!-- Table -->
  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      Нет записей для выбранного фильтра.
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-3 py-2">When</th>
            <th class="px-3 py-2">Actor</th>
            <th class="px-3 py-2">Action</th>
            <th class="px-3 py-2">Resource</th>
            <th class="px-3 py-2">Changes</th>
            <th class="px-3 py-2">IP</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as e (e.id)}
            <tr class="align-top hover:bg-slate-50">
              <td class="px-3 py-2 text-xs text-slate-500 whitespace-nowrap">
                {new Date(e.created_at).toLocaleString()}
              </td>
              <td class="px-3 py-2 text-slate-700">
                {#if e.actor}
                  <span title={e.actor.full_name ?? ''}>@{e.actor.username}</span>
                {:else}
                  <span class="text-slate-400 italic">system / anon</span>
                {/if}
              </td>
              <td class="px-3 py-2">
                <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {actionColor(e.action)}">
                  {e.action}
                </span>
              </td>
              <td class="px-3 py-2 text-xs text-slate-600">
                {#if e.resource_type}
                  <span class="rounded-md bg-slate-100 px-1.5 py-0.5">{e.resource_type}</span>
                  {#if e.resource_id !== null}<span class="ml-1 font-mono text-slate-500">#{e.resource_id}</span>{/if}
                {:else}—{/if}
              </td>
              <td class="px-3 py-2 max-w-md">
                {#if e.changes}
                  <code class="block break-all rounded bg-slate-100 px-2 py-1 text-[11px] text-slate-700">
                    {fmtChanges(e.changes)}
                  </code>
                {:else}
                  <span class="text-xs text-slate-400">—</span>
                {/if}
              </td>
              <td class="px-3 py-2 font-mono text-xs text-slate-500">{e.ip ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if hasMore}
      <div class="flex justify-center">
        <button onclick={loadMore}
                class="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Load more
        </button>
      </div>
    {/if}
  {/if}
</div>
