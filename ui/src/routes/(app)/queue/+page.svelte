<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { Activity, CheckCheck, CheckCircle2 } from 'lucide-svelte'

  import { globalQueue as queueApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { GlobalQueueSnapshot } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let snap = $state<GlobalQueueSnapshot | null>(null)
  let loading = $state(true)
  let timer: ReturnType<typeof setInterval> | null = null

  async function load(initial = false) {
    try {
      snap = await queueApi.get()
    } catch (e) {
      if (initial) showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  onMount(() => {
    load(true)
    timer = setInterval(load, 4000) // лёгкий опрос — снапшот дешёвый
  })
  onDestroy(() => {
    if (timer) clearInterval(timer)
  })

  const TASK_LABEL: Record<string, string> = {
    post: 'Пост',
    sitewide_link: 'Сквозная',
    homepage_link: 'С главной',
  }
  const TASK_CLASS: Record<string, string> = {
    post: 'bg-brand-100 text-brand-700',
    sitewide_link: 'bg-violet-100 text-violet-700',
    homepage_link: 'bg-cyan-100 text-cyan-700',
  }

  function statusClass(s: string): string {
    switch (s) {
      case 'running': return 'bg-emerald-100 text-emerald-700'
      case 'queued': return 'bg-blue-100 text-blue-700'
      case 'unpacking': return 'bg-blue-100 text-blue-700'
      case 'scheduled': return 'bg-slate-100 text-slate-600'
      case 'paused': return 'bg-amber-100 text-amber-700'
      case 'need_more_admins': return 'bg-red-100 text-red-700'
      case 'needs_review': return 'bg-orange-100 text-orange-700'
      default: return 'bg-slate-100 text-slate-500'
    }
  }
</script>

<div class="space-y-5">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Global Queue</h1>
    <p class="mt-1 text-sm text-slate-500">
      Вся активная работа в одном месте — постинг, простановка ссылок и валидация.
      Обновляется автоматически.
    </p>
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if snap}
    <!-- Лимитер: индикатор throttled -->
    <section class="rounded-lg border border-slate-200 bg-white p-5">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <Activity size={16} class="text-slate-500" />
          <h2 class="text-sm font-semibold text-slate-800">Постинг — глобальная ёмкость</h2>
        </div>
        {#if snap.limiter.throttled}
          <span class="rounded-full bg-amber-100 px-2.5 py-0.5 text-[11px] font-semibold uppercase text-amber-700">
            throttled — потолок занят
          </span>
        {:else}
          <span class="rounded-full bg-emerald-100 px-2.5 py-0.5 text-[11px] font-semibold uppercase text-emerald-700">
            есть свободные слоты
          </span>
        {/if}
      </div>
      <div class="mt-3 flex items-center gap-3">
        <div class="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100">
          <div class="h-full rounded-full transition-all"
               class:bg-amber-500={snap.limiter.throttled}
               class:bg-emerald-500={!snap.limiter.throttled}
               style="width: {snap.limiter.utilization_pct}%"></div>
        </div>
        <span class="tabular-nums text-sm font-medium text-slate-700">
          {snap.limiter.in_use} / {snap.limiter.limit}
        </span>
      </div>
      <p class="mt-2 text-[11px] text-slate-400">
        Одновременных постов через все прогоны и worker-процессы. Делится между
        активными прогонами — items разных прогонов чередуются здесь. Меняется в
        <a href="/settings" class="underline hover:text-slate-600">Settings → Global posting capacity</a>.
      </p>
    </section>

    <!-- Posting lane -->
    <section class="rounded-lg border border-slate-200 bg-white">
      <div class="flex items-center justify-between border-b border-slate-100 px-5 py-3">
        <h2 class="text-sm font-semibold text-slate-800">Прогоны постинга / ссылок</h2>
        <span class="text-xs text-slate-400">
          активных: {snap.summary.posting_active} · в работе: {snap.summary.posting_running}
        </span>
      </div>
      {#if snap.posting.length === 0}
        <div class="px-5 py-8 text-center text-sm text-slate-400">Нет активных прогонов.</div>
      {:else}
        <div class="divide-y divide-slate-100">
          {#each snap.posting as p (p.id)}
            {@const isGen = p.gen_total != null && p.gen_total > 0}
            {@const stillGen = isGen && (p.gen_done ?? 0) < (p.gen_total ?? 0)}
            {@const postedPct = p.total > 0 ? Math.round(p.posted * 100 / p.total) : 0}
            {@const genItPct = p.total > 0 ? Math.round((p.generated ?? 0) * 100 / p.total) : 0}
            {@const genAhead = Math.max(0, genItPct - postedPct)}
            <a href={`/runs/${p.id}`} class="flex items-center gap-4 px-5 py-3 hover:bg-slate-50">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase {TASK_CLASS[p.task_type] ?? 'bg-slate-100 text-slate-600'}">
                    {TASK_LABEL[p.task_type] ?? p.task_type}
                  </span>
                  <span class="truncate text-sm font-medium text-slate-800">{p.name}</span>
                  {#if stillGen}
                    <span class="rounded-full bg-orange-100 px-2 py-0.5 text-[10px] font-medium uppercase text-orange-700">генерация</span>
                  {:else}
                    <span class="rounded-full px-2 py-0.5 text-[10px] font-medium uppercase {statusClass(p.status)}">
                      {p.status}
                    </span>
                  {/if}
                </div>
                <div class="mt-1.5 flex items-center gap-3">
                  {#if isGen}
                    <!-- dual-бар: зелёный=постинг, оранжевый=сгенерировано (ждёт постинга) -->
                    <div class="flex h-1.5 w-40 overflow-hidden rounded-full bg-slate-100">
                      <div class="h-full bg-emerald-500" style="width: {postedPct}%"></div>
                      <div class="h-full bg-orange-400" style="width: {genAhead}%"></div>
                    </div>
                    <span class="text-[11px] tabular-nums">
                      <span class="text-orange-600">ген {p.generated ?? 0}</span> ·
                      <span class="text-emerald-600">пост {p.posted}</span>
                      <span class="text-slate-400">/ {p.total}</span>
                    </span>
                  {:else}
                    <div class="h-1.5 w-40 overflow-hidden rounded-full bg-slate-100">
                      <div class="h-full rounded-full bg-emerald-500" style="width: {p.progress_pct}%"></div>
                    </div>
                    <span class="text-[11px] tabular-nums text-slate-500">
                      {p.posted + p.failed + p.skipped}/{p.total} ({p.progress_pct}%)
                    </span>
                    <span class="text-[11px] text-slate-400">
                      ✓{p.posted} · ✗{p.failed} · ⤼{p.skipped}
                    </span>
                  {/if}
                </div>
              </div>
            </a>
          {/each}
        </div>
      {/if}
    </section>

    <!-- Validation lane -->
    <section class="rounded-lg border border-slate-200 bg-white p-5">
      <div class="flex items-center gap-2">
        <CheckCircle2 size={16} class="text-slate-500" />
        <h2 class="text-sm font-semibold text-slate-800">Валидация кредов</h2>
        {#if snap.validation?.running}
          <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium uppercase text-emerald-700">running</span>
        {/if}
      </div>
      {#if !snap.validation}
        <p class="mt-3 text-sm text-slate-400">Сейчас не запущена.</p>
      {:else}
        <div class="mt-3 flex items-center gap-3">
          <div class="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100">
            <div class="h-full rounded-full bg-emerald-500 transition-all" style="width: {snap.validation.progress_pct}%"></div>
          </div>
          <span class="tabular-nums text-sm font-medium text-slate-700">
            {snap.validation.done} / {snap.validation.total}
          </span>
        </div>
        <div class="mt-2 flex flex-wrap gap-4 text-[11px] text-slate-500">
          <span>scope: <code class="rounded bg-slate-100 px-1">{snap.validation.scope}</code></span>
          <span class="text-emerald-600">valid: {snap.validation.valid}</span>
          <span class="text-red-600">invalid: {snap.validation.invalid}</span>
          <span class="text-amber-600">transient: {snap.validation.transient_errors}</span>
          {#if !snap.validation.running && snap.validation.finished_at}
            <span class="text-slate-400">завершена</span>
          {/if}
        </div>
      {/if}
    </section>

    <!-- Link-check lane — перепроверка проставленных ссылок (фиолетовый тип) -->
    {#if snap.link_checks.length > 0}
      <section class="rounded-lg border border-violet-200 bg-white">
        <div class="flex items-center justify-between border-b border-violet-100 bg-violet-50/50 px-5 py-3">
          <div class="flex items-center gap-2">
            <CheckCheck size={16} class="text-violet-500" />
            <h2 class="text-sm font-semibold text-violet-800">Валидация проставленных ссылок</h2>
          </div>
          <span class="text-xs text-violet-400">активных: {snap.summary.link_check_active}</span>
        </div>
        <div class="divide-y divide-violet-50">
          {#each snap.link_checks as lc (lc.id)}
            <a href={`/runs/${lc.id}`} class="flex items-center gap-4 px-5 py-3 hover:bg-violet-50/60">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-violet-700">проверка ссылок</span>
                  <span class="truncate text-sm font-medium text-slate-800">{lc.name}</span>
                  <span class="rounded-full px-2 py-0.5 text-[10px] font-medium uppercase {lc.status === 'running' ? 'bg-violet-100 text-violet-700' : 'bg-blue-100 text-blue-700'}">{lc.status}</span>
                </div>
                <div class="mt-1.5 flex items-center gap-3">
                  <div class="h-1.5 w-40 overflow-hidden rounded-full bg-slate-100">
                    <div class="h-full rounded-full bg-violet-500 transition-all" style="width: {lc.progress_pct}%"></div>
                  </div>
                  <span class="text-[11px] tabular-nums text-slate-500">{lc.done}/{lc.total} ({lc.progress_pct}%)</span>
                  <span class="text-[11px] text-violet-600">✓ валидных {lc.valid}</span>
                </div>
              </div>
            </a>
          {/each}
        </div>
      </section>
    {/if}
  {:else}
    <div class="rounded-lg border border-red-200 bg-white p-8 text-center text-sm text-red-600">
      Не удалось загрузить очередь.
    </div>
  {/if}
</div>
