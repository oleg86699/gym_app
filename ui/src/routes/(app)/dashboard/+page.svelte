<script lang="ts">
  import { ArrowRight } from 'lucide-svelte'
  import { onDestroy, onMount } from 'svelte'

  import { dashboard as dashApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { DashboardData, DashboardRun, PostingRunStatus } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let data = $state<DashboardData | null>(null)
  let loading = $state(true)
  let pollTimer: ReturnType<typeof setInterval> | null = null

  async function refresh(initial = false) {
    if (initial) loading = true
    try {
      data = await dashApi.get()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      if (initial) loading = false
    }
  }

  onMount(async () => {
    await refresh(true)
    // poll каждые 10 сек — для активных run-ов цифры свежие, без пере-нагрузки
    pollTimer = setInterval(() => refresh(false), 10000)
  })
  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer)
  })

  function statusClass(s: PostingRunStatus): string {
    switch (s) {
      case 'unpacking':
      case 'scheduled':
      case 'queued':
        return 'bg-amber-100 text-amber-700'
      case 'running':
        return 'bg-brand-100 text-brand-700'
      case 'paused':
        return 'bg-slate-200 text-slate-700'
      case 'done':
        return 'bg-emerald-100 text-emerald-700'
      case 'failed':
      case 'cancelled':
      case 'interrupted':
        return 'bg-red-100 text-red-700'
      case 'need_more_admins':
        return 'bg-orange-100 text-orange-700'
      default:
        return 'bg-slate-100 text-slate-600'
    }
  }

  function progressPct(r: DashboardRun): number {
    if (r.total_texts === 0) return 0
    return Math.round(((r.posted_count + r.failed_count + r.skipped_count) / r.total_texts) * 100)
  }

  function fmt(ts: string | null): string {
    return ts ? new Date(ts).toLocaleString() : '—'
  }
</script>

<div class="space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Dashboard</h1>
    <p class="mt-1 text-sm text-slate-500">
      {#if data}
        Привет, <strong>@{$currentUser?.username}</strong>.
        {#if data.scope === 'all'}
          Видишь весь стек (super_admin scope).
        {:else}
          Видишь только свой scope — свои/расшаренные проекты.
        {/if}
      {:else}
        Загружаю…
      {/if}
    </p>
  </div>

  <!-- Cards -->
  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if data}
    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      <div class="rounded-lg border border-brand-200 bg-brand-50/40 p-4">
        <div class="text-[11px] uppercase tracking-wider text-brand-700">Active runs</div>
        <div class="mt-1 text-2xl font-semibold text-brand-700">{data.cards.active_runs}</div>
        <a href="/runs" class="mt-1 inline-block text-[11px] text-brand-600 hover:underline">Open Runs <ArrowRight size={14} class="inline-block align-text-bottom" /></a>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="text-[11px] uppercase tracking-wider text-slate-500">Pending texts</div>
        <div class="mt-1 text-2xl font-semibold text-slate-900">{data.cards.pending_texts}</div>
        <div class="text-[11px] text-slate-400">в активных runs</div>
      </div>
      <div class="rounded-lg border border-emerald-200 bg-emerald-50/50 p-4">
        <div class="text-[11px] uppercase tracking-wider text-emerald-700">Posts (24h)</div>
        <div class="mt-1 text-2xl font-semibold text-emerald-700">{data.cards.posts_24h}</div>
      </div>
      <div class="rounded-lg border border-red-200 bg-red-50/50 p-4">
        <div class="text-[11px] uppercase tracking-wider text-red-700">Failed (24h)</div>
        <div class="mt-1 text-2xl font-semibold text-red-700">{data.cards.failed_24h}</div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="text-[11px] uppercase tracking-wider text-slate-500" title="Домен жив + есть рабочий cred — готовы к постингу">WP sites usable</div>
        <div class="mt-1 text-2xl font-semibold text-slate-900">{data.cards.wp_sites_active}</div>
        <a href="/wp-sites" class="mt-1 inline-block text-[11px] text-brand-600 hover:underline">Manage <ArrowRight size={14} class="inline-block align-text-bottom" /></a>
      </div>
      <div class="rounded-lg border border-emerald-200 bg-emerald-50/50 p-4">
        <div class="text-[11px] uppercase tracking-wider text-emerald-700">Cred valid</div>
        <div class="mt-1 text-2xl font-semibold text-emerald-700">{data.cards.wp_credentials_valid}</div>
      </div>
    </div>

    <!-- Active runs -->
    <section>
      <div class="mb-2 flex items-baseline justify-between">
        <h2 class="text-lg font-medium text-slate-900">Active runs</h2>
        <span class="text-xs text-slate-400">{data.active_runs.length} shown · top 10 by id</span>
      </div>
      {#if data.active_runs.length === 0}
        <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">
          Нет активных прогонов.
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-3 py-2">ID</th>
                <th class="px-3 py-2">Run</th>
                <th class="px-3 py-2">Project</th>
                <th class="px-3 py-2">Creator</th>
                <th class="px-3 py-2">Status</th>
                <th class="px-3 py-2 text-center">Progress</th>
                <th class="px-3 py-2 text-center">Texts</th>
                <th class="px-3 py-2 text-center">Started</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each data.active_runs as r (r.id)}
                {@const pct = progressPct(r)}
                <tr class="hover:bg-slate-50">
                  <td class="px-3 py-2 text-slate-500">{r.id}</td>
                  <td class="px-3 py-2 font-medium text-slate-900">
                    <a href={`/runs/${r.id}`} class="hover:text-brand-600 hover:underline">{r.name}</a>
                  </td>
                  <td class="px-3 py-2 text-slate-600">
                    <a href={`/projects/${r.project.id}`} class="text-brand-600 hover:underline">{r.project.name}</a>
                  </td>
                  <td class="px-3 py-2 text-slate-600">@{r.creator?.username ?? '—'}</td>
                  <td class="px-3 py-2">
                    <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusClass(r.status)}">
                      {r.status.replace('_', ' ')}
                    </span>
                  </td>
                  <td class="px-3 py-2 text-center">
                    {#if r.total_texts > 0}
                      <div class="flex items-center gap-2">
                        <div class="h-1.5 w-24 overflow-hidden rounded-full bg-slate-200">
                          <div class="h-full bg-brand-500 transition-all" style="width: {pct}%"></div>
                        </div>
                        <span class="w-8 text-right text-xs text-slate-500">{pct}%</span>
                      </div>
                    {:else}
                      <span class="text-xs text-slate-400">—</span>
                    {/if}
                  </td>
                  <td class="px-3 py-2 text-center text-xs text-slate-600">
                    {#if r.total_texts > 0}
                      <span class="text-emerald-700">{r.posted_count}</span>
                      / <span class="text-red-600">{r.failed_count}</span>
                      / <span>{r.total_texts}</span>
                    {:else}—{/if}
                  </td>
                  <td class="px-3 py-2 text-center text-xs text-slate-500">{fmt(r.started_at)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Recent finished -->
    <section>
      <div class="mb-2 flex items-baseline justify-between">
        <h2 class="text-lg font-medium text-slate-900">Recent finished</h2>
        <span class="text-xs text-slate-400">last 10</span>
      </div>
      {#if data.recent_runs.length === 0}
        <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">
          Завершённых прогонов ещё не было.
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-3 py-2">ID</th>
                <th class="px-3 py-2">Run</th>
                <th class="px-3 py-2">Project</th>
                <th class="px-3 py-2">Status</th>
                <th class="px-3 py-2 text-center">Result</th>
                <th class="px-3 py-2 text-center">Finished</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each data.recent_runs as r (r.id)}
                <tr class="hover:bg-slate-50">
                  <td class="px-3 py-2 text-slate-500">{r.id}</td>
                  <td class="px-3 py-2 font-medium text-slate-900">
                    <a href={`/runs/${r.id}`} class="hover:text-brand-600 hover:underline">{r.name}</a>
                  </td>
                  <td class="px-3 py-2 text-slate-600">
                    <a href={`/projects/${r.project.id}`} class="text-brand-600 hover:underline">{r.project.name}</a>
                  </td>
                  <td class="px-3 py-2">
                    <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusClass(r.status)}">
                      {r.status.replace('_', ' ')}
                    </span>
                  </td>
                  <td class="px-3 py-2 text-center text-xs text-slate-600">
                    <span class="text-emerald-700">{r.posted_count}</span>
                    / <span class="text-red-600">{r.failed_count}</span>
                    / <span>{r.total_texts}</span>
                  </td>
                  <td class="px-3 py-2 text-center text-xs text-slate-500">{fmt(r.finished_at)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <p class="text-xs text-slate-400">Auto-refresh каждые 10 сек.</p>
  {/if}
</div>
