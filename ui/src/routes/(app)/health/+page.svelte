<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { Activity, Database, Server, Shield, AlertTriangle, RefreshCw } from 'lucide-svelte'

  import { dashboard as dashApi, type SystemHealth } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { showToast } from '$lib/stores/toast'

  let health = $state<SystemHealth | null>(null)
  let loading = $state(true)
  let lastUpdated = $state<Date | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null

  async function refresh() {
    try {
      health = await dashApi.systemHealth()
      lastUpdated = new Date()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  onMount(async () => {
    await refresh()
    // Health — операционный экран, обновляем каждые 5 сек.
    timer = setInterval(refresh, 5000)
  })
  onDestroy(() => { if (timer) clearInterval(timer) })

  function relTime(iso: string | null): string {
    if (!iso) return '—'
    const diff = Date.now() - new Date(iso).getTime()
    const m = Math.floor(diff / 60000)
    if (m < 1) return 'just now'
    if (m < 60) return `${m}m ago`
    const h = Math.floor(m / 60)
    return h < 24 ? `${h}h ago` : `${Math.floor(h / 24)}d ago`
  }

  // Светофор для bool-статусов
  function dot(ok: boolean): string {
    return ok ? 'bg-emerald-500' : 'bg-red-500'
  }
</script>

<div class="space-y-4">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-xl font-semibold text-slate-900">System Health</h1>
      <p class="text-sm text-slate-500">
        Инфраструктура в реальном времени · обновляется каждые 5 сек
        {#if lastUpdated}· last {relTime(lastUpdated.toISOString())}{/if}
      </p>
    </div>
    <button onclick={refresh} disabled={loading}
            class="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
      <RefreshCw size={14} class={loading ? 'animate-spin' : ''} /> Refresh
    </button>
  </div>

  {#if health}
    <!-- Service status row -->
    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-slate-500">
          <Database size={14} /> Redis
        </div>
        <div class="mt-2 flex items-center gap-2">
          <span class="inline-block h-2.5 w-2.5 rounded-full {dot(health.redis_ok)}"></span>
          <span class="text-lg font-semibold text-slate-900">{health.redis_ok ? 'OK' : 'DOWN'}</span>
        </div>
      </div>

      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-slate-500">
          <Server size={14} /> Celery queue
        </div>
        <div class="mt-2 text-lg font-semibold text-slate-900">
          {health.celery_queue_depth ?? '—'}
          <span class="text-xs font-normal text-slate-400">pending</span>
        </div>
      </div>

      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-slate-500">
          <Shield size={14} /> CF Browser
        </div>
        <div class="mt-2 flex items-center gap-2">
          <span class="inline-block h-2.5 w-2.5 rounded-full {dot(health.cf_browser_ok)}"></span>
          <span class="text-lg font-semibold text-slate-900">{health.cf_browser_ok ? 'OK' : 'OFF'}</span>
        </div>
      </div>

      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-slate-500">
          <Database size={14} /> DB pool
        </div>
        <div class="mt-2 text-lg font-semibold text-slate-900">
          {health.db_pool.checked_out ?? '?'}/{health.db_pool.size ?? '?'}
          <span class="text-xs font-normal text-slate-400">in use</span>
          {#if (health.db_pool.overflow ?? 0) > 0}
            <span class="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-700">+{health.db_pool.overflow} overflow</span>
          {/if}
        </div>
      </div>
    </div>

    <!-- Activity + proxies -->
    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="flex items-center gap-2 text-xs uppercase tracking-wider text-slate-500">
          <Activity size={14} /> Active runs
        </div>
        <div class="mt-2 text-2xl font-semibold text-slate-900">{health.runs_active}</div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="text-xs uppercase tracking-wider text-slate-500">Batches validating</div>
        <div class="mt-2 text-2xl font-semibold text-slate-900">{health.batches_validating}</div>
      </div>
      <div class="rounded-lg border border-emerald-200 bg-emerald-50/50 p-4">
        <div class="text-xs uppercase tracking-wider text-emerald-700">Proxies active</div>
        <div class="mt-2 text-2xl font-semibold text-emerald-700">
          {health.proxies.active}<span class="text-sm font-normal text-slate-400">/{health.proxies.total}</span>
        </div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="text-xs uppercase tracking-wider text-slate-500">Proxies locked / down</div>
        <div class="mt-2 text-2xl font-semibold text-slate-900">
          <span class="text-amber-600">{health.proxies.locked}</span>
          <span class="text-slate-300">/</span>
          <span class="text-red-600">{health.proxies.down}</span>
        </div>
      </div>
    </div>

    <!-- Recent failures -->
    <div class="rounded-lg border border-slate-200 bg-white">
      <div class="flex items-center gap-2 border-b border-slate-100 px-4 py-3">
        <AlertTriangle size={15} class="text-amber-500" />
        <h2 class="text-sm font-medium text-slate-900">Recent posting failures (24h)</h2>
        <span class="text-xs text-slate-400">{health.recent_failures.length}</span>
      </div>
      {#if health.recent_failures.length === 0}
        <div class="px-4 py-6 text-center text-sm text-slate-400">No failures in the last 24h 🎉</div>
      {:else}
        <table class="min-w-full text-sm">
          <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
            <tr>
              <th class="px-4 py-2">When</th>
              <th class="px-4 py-2">Domain</th>
              <th class="px-4 py-2">Run</th>
              <th class="px-4 py-2">Error</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            {#each health.recent_failures as f (f.text_item_id)}
              <tr class="hover:bg-slate-50">
                <td class="px-4 py-2 text-xs text-slate-500">{relTime(f.at)}</td>
                <td class="px-4 py-2 font-mono text-xs">{f.domain ?? '—'}</td>
                <td class="px-4 py-2 text-xs">
                  <a href={`/runs/${f.run_id}`} class="text-brand-600 hover:underline">#{f.run_id}</a>
                </td>
                <td class="px-4 py-2 text-xs text-red-600 max-w-md truncate" title={f.error}>{f.error}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {:else if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-400">Loading…</div>
  {/if}
</div>
