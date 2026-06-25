<script lang="ts">
  import { ArrowLeft } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import { projects as projectsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { runModeLabel } from '$lib/runLabels'
  import { prettyUrl } from '$lib/url'
  import type { DomainPlacement, DomainRunRow, DomainSummary } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let projectId = $derived(Number(page.params.id))
  let domain = $derived(page.params.domain ?? '')

  let summary = $state<DomainSummary | null>(null)
  let runs = $state<DomainRunRow[]>([])
  let placements = $state<DomainPlacement[]>([])
  let loading = $state(true)

  // Аналитика анкоров/ссылок
  let expanded = $state<string | null>(null) // 'a:'+key | 'l:'+key
  let onlyVerified = $state(false)
  let query = $state('')
  let anchorSort = $state<Sort>({ col: 'count', dir: 'desc' })
  let linkSort = $state<Sort>({ col: 'count', dir: 'desc' })

  async function load() {
    loading = true
    try {
      const [s, r, p] = await Promise.all([
        projectsApi.domainSummary(projectId, domain),
        projectsApi.domainRuns(projectId, domain),
        projectsApi.domainPlacements(projectId, domain),
      ])
      summary = s
      runs = r
      placements = p
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(load)

  type LinkType = 'post' | 'sitewide_link' | 'homepage_link'
  const TYPE_META: Record<LinkType, { label: string; cls: string }> = {
    post: { label: 'пост', cls: 'bg-slate-100 text-slate-600' },
    homepage_link: { label: '🏠 с главной', cls: 'bg-blue-100 text-blue-700' },
    sitewide_link: { label: '🔗 сквозная', cls: 'bg-purple-100 text-purple-700' },
  }
  const TYPE_ORDER: LinkType[] = ['post', 'homepage_link', 'sitewide_link']

  interface PlacementGroup {
    key: string
    count: number
    verified: number
    types: Record<LinkType, number>
    items: DomainPlacement[]
  }
  type Sort = { col: 'key' | 'count'; dir: 'asc' | 'desc' }

  function buildGroups(rows: DomainPlacement[], keyOf: (p: DomainPlacement) => string): PlacementGroup[] {
    const map = new Map<string, PlacementGroup>()
    for (const p of rows) {
      if (onlyVerified && p.verified !== true) continue
      const key = keyOf(p)
      let g = map.get(key)
      if (!g) {
        g = { key, count: 0, verified: 0, types: { post: 0, homepage_link: 0, sitewide_link: 0 }, items: [] }
        map.set(key, g)
      }
      g.count++
      if (p.verified === true) g.verified++
      g.types[p.type] = (g.types[p.type] ?? 0) + 1
      g.items.push(p)
    }
    return [...map.values()]
  }

  function sortFilter(groups: PlacementGroup[], sort: Sort, q: string): PlacementGroup[] {
    const ql = q.trim().toLowerCase()
    const rows = ql ? groups.filter((g) => g.key.toLowerCase().includes(ql)) : groups
    const dir = sort.dir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) =>
      sort.col === 'key'
        ? dir * a.key.localeCompare(b.key)
        : dir * (a.count - b.count) || a.key.localeCompare(b.key),
    )
  }

  let anchorGroups = $derived(buildGroups(placements, (p) => p.anchor || '∅ без анкора'))
  let linkGroups = $derived(buildGroups(placements, (p) => p.link_url || '∅ без ссылки'))
  let anchorRows = $derived(sortFilter(anchorGroups, anchorSort, query))
  let linkRows = $derived(sortFilter(linkGroups, linkSort, query))
  let hasData = $derived(anchorGroups.length > 0 || linkGroups.length > 0)

  function toggle(ns: 'a' | 'l', key: string) {
    const k = `${ns}:${key}`
    expanded = expanded === k ? null : k
  }
  function toggleSort(which: 'a' | 'l', col: 'key' | 'count') {
    const cur = which === 'a' ? anchorSort : linkSort
    const next: Sort =
      cur.col === col
        ? { col, dir: cur.dir === 'asc' ? 'desc' : 'asc' }
        : { col, dir: col === 'count' ? 'desc' : 'asc' }
    if (which === 'a') anchorSort = next
    else linkSort = next
  }
  function sortIcon(sort: Sort, col: 'key' | 'count'): string {
    if (sort.col !== col) return '↕'
    return sort.dir === 'asc' ? '▲' : '▼'
  }
  function host(url: string | null): string {
    if (!url) return '—'
    try {
      return new URL(url).hostname.replace(/^www\./, '')
    } catch {
      return url
    }
  }

  function runStatusClass(s: string): string {
    switch (s) {
      case 'done': return 'bg-emerald-100 text-emerald-700'
      case 'running': case 'queued': return 'bg-blue-100 text-blue-700'
      case 'ready': case 'scheduled': return 'bg-indigo-100 text-indigo-700'
      case 'failed': case 'interrupted': case 'need_more_admins': return 'bg-red-100 text-red-700'
      case 'paused': return 'bg-amber-100 text-amber-700'
      case 'cancelled': return 'bg-slate-200 text-slate-600'
      default: return 'bg-slate-100 text-slate-500'
    }
  }
  function pct(r: DomainRunRow): number {
    return r.total ? Math.min(100, Math.round(((r.posted + r.failed) / r.total) * 100)) : 0
  }
</script>

<div class="space-y-4">
  <div>
    <a href={`/projects/${projectId}`} class="text-sm text-slate-500 hover:text-slate-700">
      <ArrowLeft size={14} class="inline-block align-text-bottom" /> Проект
    </a>
    <div class="mt-1 flex items-center gap-3">
      <h1 class="text-2xl font-semibold text-slate-900">{domain}</h1>
      <span class="rounded-full bg-indigo-100 px-2 py-0.5 text-[11px] font-medium text-indigo-700">целевой домен</span>
    </div>
    <p class="mt-1 text-sm text-slate-500">Бэклинки на этот домен + аналитика постинга в рамках проекта.</p>
  </div>

  <!-- Сводка: компактный стат-бар -->
  {#if summary}
    <div class="rounded-lg border border-slate-200 bg-white px-5 py-3.5">
      <div class="flex flex-wrap items-center gap-x-8 gap-y-3">
        {#each [
          ['Всего', summary.total, 'text-slate-900'],
          ['Опубликовано', summary.posted, 'text-emerald-600'],
          ['Ошибки', summary.failed, 'text-red-600'],
          ['Пропущено', summary.skipped, 'text-slate-500'],
          ['В работе', summary.in_progress, 'text-blue-600'],
          ['Сайтов', summary.sites, 'text-slate-900'],
          ['Прогонов', summary.runs, 'text-slate-900'],
        ] as [label, value, cls]}
          <div class="min-w-[56px]">
            <div class="text-xl font-semibold leading-tight {cls}">{value}</div>
            <div class="mt-0.5 text-[11px] uppercase tracking-wide text-slate-400">{label}</div>
          </div>
        {/each}
        <div class="ml-auto border-l border-slate-100 pl-6">
          <div class="text-xl font-semibold leading-tight text-indigo-600">{summary.total ? Math.round((summary.posted / summary.total) * 100) : 0}%</div>
          <div class="mt-0.5 text-[11px] uppercase tracking-wide text-slate-400">конверсия</div>
        </div>
      </div>
      {#if summary.last_posted_at}
        <p class="mt-3 border-t border-slate-100 pt-2 text-[11px] text-slate-400">
          Последняя публикация: {new Date(summary.last_posted_at).toLocaleString()}
        </p>
      {/if}
    </div>
    <div class="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-5 py-3">
      <div class="flex items-baseline gap-2">
        <span class="text-2xl font-semibold text-emerald-700">{summary.available_sites}</span>
        <span class="text-sm text-emerald-600">из {summary.pool_total} сайтов пула свободно</span>
      </div>
      <p class="mt-0.5 text-[11px] text-emerald-700/80">
        Уникальных сайтов, на которых ещё НЕ стоит пост со ссылкой на этот домен. Столько
        постов можно сделать на этот домен при max posts/site = 1.
      </p>
    </div>
  {/if}

  <!-- Прогоны по домену -->
  <div>
    <h2 class="mb-2 text-lg font-semibold text-slate-900">Прогоны по домену</h2>
    {#if loading}
      <p class="text-sm text-slate-500">Загрузка…</p>
    {:else if runs.length === 0}
      <p class="rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-500">Прогонов по этому домену пока нет.</p>
    {:else}
      <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
            <tr>
              <th class="px-4 py-2">ID</th>
              <th class="px-4 py-2">Name</th>
              <th class="px-4 py-2">Status</th>
              <th class="px-4 py-2">Progress</th>
              <th class="px-4 py-2 text-center">Texts (по домену)</th>
              <th class="px-4 py-2">Created</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            {#each runs as r (r.id)}
              {@const p = pct(r)}
              <tr class="hover:bg-slate-50">
                <td class="px-4 py-2 text-slate-500">{r.id}</td>
                <td class="px-4 py-2 font-medium text-slate-900">
                  <a href={`/runs/${r.id}`} class="hover:text-brand-600 hover:underline">{r.name}</a>
                  <span class="mt-0.5 block text-[11px] font-normal text-indigo-600">{runModeLabel(r)}</span>
                </td>
                <td class="px-4 py-2">
                  <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {runStatusClass(r.status)}">{r.status.replace('_', ' ')}</span>
                </td>
                <td class="px-4 py-2">
                  <div class="flex items-center gap-2">
                    <div class="h-1.5 w-24 overflow-hidden rounded-full bg-slate-200">
                      <div class="h-full bg-brand-500 transition-all" style="width: {p}%"></div>
                    </div>
                    <span class="w-8 text-right text-xs text-slate-500">{p}%</span>
                  </div>
                </td>
                <td class="px-4 py-2 text-center text-xs text-slate-600">
                  <span class="text-emerald-700">{r.posted}</span>
                  / <span class="text-red-600">{r.failed}</span>
                  / <span>{r.total}</span>
                </td>
                <td class="px-4 py-2 text-xs text-slate-500">{new Date(r.created_at).toLocaleString()}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <!-- Аналитика: анкоры и ссылки -->
  <div>
    <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-lg font-semibold text-slate-900">Анкоры и ссылки</h2>
      <div class="flex items-center gap-3">
        <input
          type="search"
          bind:value={query}
          placeholder="поиск по анкору или ссылке…"
          class="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm placeholder:text-slate-400 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
        <label class="flex items-center gap-1.5 whitespace-nowrap text-xs text-slate-500">
          <input type="checkbox" bind:checked={onlyVerified} class="rounded border-slate-300" />
          только ✓ проверенные
        </label>
      </div>
    </div>

    {#snippet tableCard(title: string, nameHeader: string, rows: PlacementGroup[], ns: 'a' | 'l', sort: Sort)}
      <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <div class="flex items-center gap-2 border-b border-slate-100 bg-slate-50/60 px-4 py-2">
          <h3 class="text-sm font-semibold text-slate-700">{title}</h3>
          <span class="rounded-full bg-slate-200 px-1.5 text-[11px] font-medium text-slate-600">{rows.length}</span>
        </div>
        <table class="min-w-full text-sm">
          <thead class="text-left text-xs uppercase tracking-wider text-slate-500">
            <tr class="border-b border-slate-100">
              <th class="cursor-pointer select-none px-4 py-2 hover:text-slate-700" onclick={() => toggleSort(ns, 'key')}>
                {nameHeader} <span class="text-slate-400">{sortIcon(sort, 'key')}</span>
              </th>
              <th class="cursor-pointer select-none whitespace-nowrap px-4 py-2 text-right hover:text-slate-700" onclick={() => toggleSort(ns, 'count')}>
                Размещения <span class="text-slate-400">{sortIcon(sort, 'count')}</span>
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            {#each rows as g (g.key)}
              <tr class="cursor-pointer hover:bg-slate-50" onclick={() => toggle(ns, g.key)}>
                <td class="px-4 py-2 align-top font-medium">
                  <span class="text-slate-300">{expanded === `${ns}:${g.key}` ? '▾' : '▸'}</span>
                  <span class="break-all text-brand-600">{g.key}</span>
                </td>
                <td class="px-4 py-2 align-top">
                  <div class="flex items-center justify-end gap-2">
                    <span class="w-7 text-right font-semibold tabular-nums text-slate-900">{g.count}</span>
                    <div class="flex flex-wrap justify-end gap-1">
                      {#each TYPE_ORDER as t}
                        {#if g.types[t]}
                          <span class="rounded px-1.5 py-0.5 text-[11px] font-medium {TYPE_META[t].cls}">{TYPE_META[t].label} {g.types[t]}</span>
                        {/if}
                      {/each}
                    </div>
                    <span class="w-7 shrink-0 text-right text-[11px] font-medium text-emerald-600" title="подтверждено проверкой">{g.verified ? `✓${g.verified}` : ''}</span>
                  </div>
                </td>
              </tr>
              {#if expanded === `${ns}:${g.key}`}
                <tr class="bg-slate-50/60">
                  <td colspan="2" class="px-3 pb-3 pt-1">
                    <div class="overflow-hidden rounded border border-slate-200 bg-white">
                      <table class="w-full table-fixed text-xs">
                        <thead class="bg-slate-50 text-left uppercase tracking-wide text-slate-400">
                          <tr>
                            <th class="px-2 py-1 font-medium">Анкор</th>
                            <th class="px-2 py-1 font-medium">Целевая ссылка</th>
                            <th class="px-2 py-1 font-medium">Размещено на</th>
                            <th class="w-14 px-2 py-1 font-medium">Тип</th>
                            <th class="w-12 px-2 py-1 text-center font-medium">Валид.</th>
                            <th class="w-20 px-2 py-1 font-medium">Дата</th>
                          </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-100">
                          {#each g.items as it, i (i)}
                            <tr class="align-top">
                              <td class="break-words px-2 py-1 text-slate-700">{it.anchor || '—'}</td>
                              <td class="px-2 py-1">
                                {#if it.link_url}
                                  <a href={it.link_url} target="_blank" rel="noopener" class="break-all text-slate-600 hover:underline" onclick={(e) => e.stopPropagation()}>{it.link_url}</a>
                                {:else}<span class="text-slate-400">—</span>{/if}
                              </td>
                              <td class="px-2 py-1">
                                {#if it.posted_url}
                                  <a href={it.posted_url} target="_blank" rel="noopener" class="break-all text-brand-600 hover:underline" title={prettyUrl(it.posted_url)} onclick={(e) => e.stopPropagation()}>{host(it.posted_url)}</a>
                                {:else}<span class="text-slate-400">— не размещено —</span>{/if}
                              </td>
                              <td class="px-2 py-1">
                                <span class="rounded px-1.5 py-0.5 text-[10px] font-medium {TYPE_META[it.type].cls}">{TYPE_META[it.type].label}</span>
                              </td>
                              <td class="px-2 py-1 text-center">
                                {#if it.verified === true}<span class="text-emerald-600" title="подтверждена на странице">✓</span>
                                {:else if it.verified === false}<span class="text-red-500" title="не найдена при проверке">✗</span>
                                {:else}<span class="text-slate-300" title="не проверялось">—</span>{/if}
                              </td>
                              <td class="whitespace-nowrap px-2 py-1 text-slate-400">{it.posted_at ? new Date(it.posted_at).toLocaleDateString() : '—'}</td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                  </td>
                </tr>
              {/if}
            {/each}
            {#if rows.length === 0}
              <tr><td colspan="2" class="px-4 py-3 text-center text-xs text-slate-400">ничего не найдено</td></tr>
            {/if}
          </tbody>
        </table>
      </div>
    {/snippet}

    {#if loading}
      <p class="text-sm text-slate-500">Загрузка…</p>
    {:else if !hasData}
      <p class="rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-500">
        {onlyVerified ? 'Проверенных размещений пока нет.' : 'Размещённых ссылок на этот домен пока нет.'}
      </p>
    {:else}
      <div class="grid gap-4 xl:grid-cols-2">
        {@render tableCard('Анкоры', 'Анкор', anchorRows, 'a', anchorSort)}
        {@render tableCard('Целевые ссылки', 'Ссылка', linkRows, 'l', linkSort)}
      </div>
      <p class="mt-2 text-[11px] leading-relaxed text-slate-400">
        Клик по строке — все размещения (где стоит → на какую ссылку / каким анкором). Типы:
        <span class="font-medium text-slate-600">пост</span> ·
        <span class="font-medium text-blue-700">🏠 с главной</span> ·
        <span class="font-medium text-purple-700">🔗 сквозная</span>.
        <span class="text-emerald-600">✓N</span> — подтверждено проверкой.
      </p>
    {/if}
  </div>
</div>
