<script lang="ts">
  import { ArrowLeft } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import { projects as projectsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { runModeLabel } from '$lib/runLabels'
  import type { DomainRunRow, DomainSummary } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let projectId = $derived(Number(page.params.id))
  let domain = $derived(page.params.domain ?? '')

  let summary = $state<DomainSummary | null>(null)
  let runs = $state<DomainRunRow[]>([])
  let loading = $state(true)

  async function load() {
    loading = true
    try {
      const [s, r] = await Promise.all([
        projectsApi.domainSummary(projectId, domain),
        projectsApi.domainRuns(projectId, domain),
      ])
      summary = s
      runs = r
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(load)

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
</div>
