<script lang="ts">
  import { ArrowRight, Download, HelpCircle, Pause, Play, RotateCw, X } from 'lucide-svelte'
  import { onDestroy, onMount } from 'svelte'

  import { proxies as proxiesApi, wpBatches as batchesApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { Proxy, WpBatch, WpBatchStatus } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let items = $state<WpBatch[]>([])
  let loading = $state(true)
  let proxiesList = $state<Proxy[]>([])
  let pollTimer: ReturnType<typeof setInterval> | null = null

  let isSuper = $derived($currentUser?.is_super_admin ?? false)

  // 1-sec tick для elapsed / ETA в строках с status=validating.
  let nowMs = $state(Date.now())
  let etaTimer: ReturnType<typeof setInterval> | null = null

  function fmtDuration(ms: number): string {
    if (!isFinite(ms) || ms < 0) return '—'
    const s = Math.round(ms / 1000)
    if (s < 60) return `${s}s`
    const m = Math.floor(s / 60); const sr = s % 60
    if (m < 60) return sr > 0 ? `${m}m ${sr}s` : `${m}m`
    const h = Math.floor(m / 60); const mr = m % 60
    return mr > 0 ? `${h}h ${mr}m` : `${h}h`
  }
  function batchElapsedMs(b: WpBatch): number {
    return b.validation_started_at ? nowMs - new Date(b.validation_started_at).getTime() : 0
  }
  function batchDoneCount(b: WpBatch): number {
    return (b.valid_count ?? 0) + (b.invalid_count ?? 0) + (b.transient_count ?? 0)
      + (b.duplicate_credentials ?? 0)
  }
  function batchEtaMs(b: WpBatch): number {
    const elapsed = batchElapsedMs(b)
    const done = batchDoneCount(b)
    if (elapsed <= 1000 || done <= 0) return Infinity
    const rate = done / (elapsed / 1000)
    return Math.max(0, ((b.total_credentials ?? 0) - done) / rate) * 1000
  }

  async function refresh() {
    try {
      const res = await batchesApi.list()
      items = res.items
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  async function loadProxies() {
    try {
      const r = await proxiesApi.list({ status: 'active', limit: 200 })
      proxiesList = r.items
    } catch {
      proxiesList = []
    }
  }

  function hasActive(): boolean {
    return items.some((b) => b.status === 'validating')
  }
  // Polling: каждые 3 сек если есть активная валидация (live progress);
  // каждые 15 сек иначе — ловим переходы (внешний CLI старт / ready→validating).
  let tickCounter = 0
  function tickPoll() {
    tickCounter += 1
    if (hasActive() || tickCounter % 5 === 0) refresh()
  }

  onMount(async () => {
    await refresh()
    pollTimer = setInterval(tickPoll, 3000)
    etaTimer = setInterval(() => { nowMs = Date.now() }, 1000)
  })
  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer)
    if (etaTimer) clearInterval(etaTimer)
  })

  function statusBadge(s: WpBatchStatus): string {
    switch (s) {
      case 'uploaded': return 'bg-slate-100 text-slate-600'
      case 'validating': return 'bg-brand-100 text-brand-700'
      case 'paused': return 'bg-amber-100 text-amber-700'
      case 'done': return 'bg-emerald-100 text-emerald-700'
      default: return 'bg-slate-100 text-slate-500'
    }
  }

  function fmt(d: string | null): string {
    return d ? new Date(d).toLocaleString() : '—'
  }

  function progressPct(b: WpBatch): number {
    if (b.total_credentials === 0) return 0
    // duplicates skipped валидатором — считаем "обработанными" чтобы прогресс
    // не застревал на 97% когда 3 cred задублились и не валидировались.
    const dups = b.duplicate_credentials ?? 0
    const done = b.valid_count + b.invalid_count + b.transient_count + dups
    return Math.round((done / b.total_credentials) * 100)
  }

  // ─── Create modal ──────────────────────────────────────────────────

  let helpOpen = $state(false)   // инструкция по странице
  let createOpen = $state(false)
  let createBusy = $state(false)
  let newFile = $state<File | null>(null)
  let newName = $state('')
  let newTag = $state('')
  let newNote = $state('')
  let newCost = $state<string>('')
  let newCurrency = $state('USD')

  function openCreate() {
    newFile = null
    newName = `Batch ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`
    newTag = ''
    newNote = ''
    newCost = ''
    newCurrency = 'USD'
    createOpen = true
  }

  async function submitCreate(e: SubmitEvent) {
    e.preventDefault()
    if (!newFile) { showToast('error', 'Select CSV file'); return }
    createBusy = true
    try {
      const res = await batchesApi.create(newFile, {
        name: newName,
        tag: newTag || undefined,
        note: newNote || undefined,
        cost_total: newCost ? Number(newCost) : undefined,
        cost_currency: newCost ? newCurrency : undefined,
        auto_validate: false,
        auto_provision: false,
      })
      showToast(
        'success',
        `Batch #${res.batch_id}: ${res.credentials_new} new, ${res.credentials_duplicate} duplicates, ${res.sites_created} new sites` +
          ' · запусти проверку кнопкой Validate',
      )
      createOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      createBusy = false
    }
  }

  // ─── Validate modal ────────────────────────────────────────────────

  let validateFor = $state<WpBatch | null>(null)
  let vScope = $state<'all' | 'invalid' | 'pending'>('all')
  let vConcurrency = $state(5)
  let vProxyId = $state<number | null>(null)
  let vDetectLang = $state(true)
  let validateBusy = $state(false)

  function openValidate(b: WpBatch) {
    validateFor = b
    vScope = 'all'
    vConcurrency = 5
    vProxyId = null
    vDetectLang = true
  }

  async function submitValidate(e: SubmitEvent) {
    e.preventDefault()
    if (!validateFor) return
    validateBusy = true
    try {
      await batchesApi.validate(validateFor.id, {
        scope: vScope,
        concurrency: vConcurrency,
        proxy_id: vProxyId,
        detect_language: vDetectLang,
      })
      showToast('success', `Validation started (scope: ${vScope})`)
      validateFor = null
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      validateBusy = false
    }
  }

  async function doPause(b: WpBatch) {
    try { await batchesApi.pause(b.id); showToast('success', 'Pause requested'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
  async function doResume(b: WpBatch) {
    try { await batchesApi.resume(b.id); showToast('success', 'Resumed'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
  async function doRevalidateFailed(b: WpBatch) {
    try { await batchesApi.revalidateFailed(b.id); showToast('success', 'Re-validation queued'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
  async function doDelete(b: WpBatch) {
    if (!confirm(`Удалить batch "${b.name}"? Credentials в пуле останутся, но потеряют привязку к этой пачке.`)) return
    try { await batchesApi.remove(b.id); showToast('success', 'Deleted'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
</script>

<div class="space-y-4">
  <div class="flex items-start justify-between gap-4">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Import batches ({items.length})</h1>
      {#if isSuper}
        <p class="mt-1 text-sm text-slate-500">
          Загружаешь CSV <ArrowRight size={14} class="inline-block align-text-bottom" /> создаётся batch <ArrowRight size={14} class="inline-block align-text-bottom" /> проверка <ArrowRight size={14} class="inline-block align-text-bottom" /> отчёт. Доступы попадают в общий пул
          <a href="/wp-sites" class="text-brand-600 hover:underline">/wp-sites</a>.
        </p>
      {/if}
    </div>
    <div class="flex shrink-0 items-center gap-2">
      <button type="button" onclick={() => (helpOpen = true)}
              title="Инструкция: формат файла, колонки, статусы, действия"
              aria-label="Инструкция по странице"
              class="inline-flex items-center justify-center rounded-md border border-slate-300 p-1.5 text-slate-600 hover:bg-slate-50">
        <HelpCircle size={18} />
      </button>
      <button onclick={openCreate}
              class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
        + New batch (CSV)
      </button>
    </div>
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      Нет батчей. Загрузи первый файл.
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-3 py-2">#</th>
            <th class="px-3 py-2">Name</th>
            <th class="px-3 py-2">Status</th>
            <th class="px-3 py-2 text-center">Progress</th>
            <th class="px-3 py-2 text-center">Valid / Total</th>
            <th class="px-3 py-2">Tag</th>
            <th class="px-3 py-2">Cost</th>
            <th class="px-3 py-2">Created</th>
            <th class="px-3 py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as b (b.id)}
            {@const pct = progressPct(b)}
            <tr class="align-top hover:bg-slate-50">
              <td class="px-3 py-2 text-slate-500">{b.id}</td>
              <td class="px-3 py-2">
                <a href={`/batches/${b.id}`} class="font-medium text-slate-900 hover:text-brand-600 hover:underline">{b.name}</a>
                {#if b.source_filename}<div class="text-[11px] text-slate-400">{b.source_filename}</div>{/if}
                {#if b.note}<div class="mt-0.5 text-[11px] text-slate-500">{b.note}</div>{/if}
              </td>
              <td class="px-3 py-2">
                <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusBadge(b.status)}">
                  {b.status}{#if b.pause_requested && b.status === 'validating'} (pausing){/if}
                </span>
              </td>
              <td class="px-3 py-2 text-center">
                {#if b.total_credentials > 0}
                  {@const isLive = b.status === 'validating' && !b.pause_requested}
                  {@const elapsed = batchElapsedMs(b)}
                  {@const eta = batchEtaMs(b)}
                  <div class="flex items-center gap-2">
                    {#if isLive}
                      <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-brand-600" title="Validation in progress"></span>
                    {/if}
                    <div class="h-1.5 w-24 overflow-hidden rounded-full bg-slate-200 flex">
                      <div class="h-full bg-brand-500 transition-all" style="width: {pct}%"></div>
                      {#if isLive && pct < 100}
                        <div class="h-full bar-stripes-mini" style="width: {100 - pct}%" title="In progress"></div>
                      {/if}
                    </div>
                    <span class="w-8 text-right text-xs text-slate-500">{pct}%</span>
                  </div>
                  {#if isLive}
                    <div class="mt-1 text-[10px] text-slate-500">
                      {fmtDuration(elapsed)}
                      {#if isFinite(eta) && eta > 0} · ~{fmtDuration(eta)} left{/if}
                    </div>
                  {:else if b.status === 'done' && b.validation_started_at && b.validation_finished_at}
                    <div class="mt-1 text-[10px] text-slate-400">
                      took {fmtDuration(new Date(b.validation_finished_at).getTime() - new Date(b.validation_started_at).getTime())}
                    </div>
                  {/if}
                {:else}
                  <span class="text-xs text-slate-400">—</span>
                {/if}
              </td>
              <td class="px-3 py-2 text-center text-xs">
                <span class="text-emerald-700 font-semibold">{b.valid_count}</span>
                <span class="text-slate-400"> / {b.total_credentials}</span>
                {#if b.invalid_count > 0}
                  <span class="text-red-600"> · <X size={14} class="inline-block align-text-bottom" /> {b.invalid_count}</span>
                {/if}
                {#if b.duplicate_credentials > 0}
                  <div class="text-[11px] text-amber-600">(dup: {b.duplicate_credentials})</div>
                {/if}
              </td>
              <td class="px-3 py-2 text-xs">
                {#if b.tag}<span class="rounded-md bg-slate-100 px-1.5 py-0.5">{b.tag}</span>{:else}—{/if}
              </td>
              <td class="px-3 py-2 text-xs text-slate-700">
                {#if b.cost_total != null}
                  {b.cost_total} {b.cost_currency ?? ''}
                  {#if b.valid_count > 0}
                    <div class="text-[10px] text-slate-400">
                      {(b.cost_total / b.valid_count).toFixed(3)} per valid
                    </div>
                  {/if}
                {:else}—{/if}
              </td>
              <td class="px-3 py-2 text-xs text-slate-500">{fmt(b.created_at)}</td>
              <td class="px-3 py-2 text-right">
                {#if isSuper}
                  <div class="inline-flex flex-wrap items-center justify-end gap-2 text-xs">
                    {#if b.status === 'uploaded' || b.status === 'done'}
                      <button onclick={() => openValidate(b)} class="text-brand-600 hover:underline"><Play size={14} class="inline-block align-text-bottom" /> Validate</button>
                    {/if}
                    {#if b.status === 'validating' && !b.pause_requested}
                      <button onclick={() => doPause(b)} class="text-amber-700 hover:underline"><Pause size={14} class="inline-block align-text-bottom" /> Pause</button>
                    {/if}
                    {#if b.status === 'paused'}
                      <button onclick={() => doResume(b)} class="text-brand-600 hover:underline"><Play size={14} class="inline-block align-text-bottom" /> Resume</button>
                    {/if}
                    {#if b.status === 'done' && b.invalid_count > 0}
                      <button onclick={() => doRevalidateFailed(b)} class="text-slate-600 hover:underline"><RotateCw size={14} class="inline-block align-text-bottom" /> Re-failed</button>
                    {/if}
                    <a href={`/admin/api/batches/${b.id}/result.csv`} download={`batch-${b.id}-result.csv`}
                       class="text-slate-600 hover:underline"><Download size={14} class="inline-block align-text-bottom" /> CSV</a>
                    <a href={`/admin/api/batches/${b.id}/result.csv?include_password=true`} download={`batch-${b.id}-with-passwords.csv`}
                       title="⚠ CSV с расшифрованными паролями"
                       class="text-slate-600 hover:underline"><Download size={14} class="inline-block align-text-bottom" />+pw</a>
                    <button onclick={() => doDelete(b)} class="text-red-600 hover:underline"><X size={14} class="inline-block align-text-bottom" /> Delete</button>
                  </div>
                {:else}<a href="/batches/{b.id}" class="text-xs text-brand-600 hover:underline">Открыть →</a>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if hasActive()}
    <p class="text-xs text-slate-400">Auto-refresh каждые 3 сек пока идёт валидация.</p>
  {/if}
</div>

<!-- Help: инструкция по странице -->
{#if helpOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (helpOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg bg-white shadow-xl" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-start justify-between border-b border-slate-100 px-6 py-4">
        <h2 class="text-lg font-semibold text-slate-900">Как работают батчи</h2>
        <button type="button" onclick={() => (helpOpen = false)} class="text-slate-400 hover:text-slate-700">✕</button>
      </div>

      <div class="space-y-4 overflow-auto px-6 py-5 text-sm text-slate-700">
        <p class="text-slate-600">
          Загружаешь файл с WP-доступами <ArrowRight size={13} class="inline-block align-text-bottom" /> создаётся
          <b>batch</b> <ArrowRight size={13} class="inline-block align-text-bottom" /> каждый доступ проверяется
          <ArrowRight size={13} class="inline-block align-text-bottom" /> валидные попадают в общий пул
          <a href="/wp-sites" class="text-brand-600 hover:underline">/wp-sites</a> (оттуда их берут прогоны постинга).
        </p>

        <section>
          <h3 class="font-semibold text-slate-900">Формат файла</h3>
          <ul class="mt-1.5 space-y-1 text-slate-600">
            <li><b>.csv</b> с заголовком: <code class="rounded bg-slate-100 px-1">domain,login,password</code></li>
            <li><b>.txt</b> через табы: <code class="rounded bg-slate-100 px-1">domain⇥url⇥[num]⇥login⇥password</code> (url и числа игнорируются)</li>
          </ul>
          <p class="mt-1 text-[12px] text-slate-500">Дубликаты по (сайт, логин) в пуле не задвоятся — попадут в <code>dup</code>.</p>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">Что делает проверка (Validate)</h3>
          <p class="mt-1.5 text-slate-600">
            XML-RPC <ArrowRight size={12} class="inline-block align-text-bottom" /> вход в wp-admin
            <ArrowRight size={12} class="inline-block align-text-bottom" /> роль
            <ArrowRight size={12} class="inline-block align-text-bottom" /> возможности. Опц. <b>provision</b> — заводит
            наш author-аккаунт на admin-сайтах. При создании можно сразу запустить полный цикл; позже — кнопкой
            <b>Validate</b> (scope: все / только invalid / pending, concurrency, прокси).
          </p>
        </section>

        <div class="grid gap-4 sm:grid-cols-2">
          <section>
            <h3 class="font-semibold text-slate-900">Колонки</h3>
            <ul class="mt-1.5 space-y-1 text-slate-600">
              <li><b>Progress</b> — % проверенных доступов; во время проверки — таймер и ~ETA.</li>
              <li><b>Valid / Total</b> — прошли проверку из всех; красным <X size={12} class="inline-block align-text-bottom" />N — невалидные; <span class="text-amber-600">(dup: N)</span> — дубликаты.</li>
              <li><b>Cost</b> — цена батча и цена за один валидный доступ.</li>
            </ul>
          </section>

          <section>
            <h3 class="font-semibold text-slate-900">Статусы</h3>
            <ul class="mt-1.5 space-y-1 text-slate-600">
              <li><span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-slate-600">uploaded</span> — загружен, не проверен</li>
              <li><span class="rounded-full bg-brand-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-brand-700">validating</span> — идёт проверка (страница сама обновляется каждые 3 сек)</li>
              <li><span class="rounded-full bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-amber-700">paused</span> — на паузе</li>
              <li><span class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-emerald-700">done</span> — проверка завершена</li>
            </ul>
          </section>
        </div>

        <section>
          <h3 class="font-semibold text-slate-900">Действия</h3>
          <ul class="mt-1.5 space-y-1 text-slate-600">
            <li><b>Validate</b> — запустить/перезапустить проверку · <b>Pause / Resume</b> — пауза и продолжить</li>
            <li><b>Re-failed</b> — перепроверить только невалидные · <b>CSV</b> — отчёт, <b>+pw</b> — с расшифрованными паролями ⚠</li>
            <li><b>Delete</b> — удалить batch (доступы в пуле останутся, но потеряют привязку к пачке)</li>
          </ul>
        </section>
      </div>

      <div class="flex justify-end border-t border-slate-100 px-6 py-4">
        <button type="button" onclick={() => (helpOpen = false)}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Понятно</button>
      </div>
    </div>
  </div>
{/if}

<!-- Create modal -->
{#if createOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">New import batch</h2>
      <div class="mt-1 text-xs text-slate-500">
        Поддерживается 2 формата:
        <ul class="mt-1 space-y-0.5 pl-3">
          <li>· <strong>.csv</strong> с header: <code class="rounded bg-slate-100 px-1">domain,login,password</code></li>
          <li>· <strong>.txt</strong> tab-separated: <code class="rounded bg-slate-100 px-1">domain⇥url⇥[num]⇥login⇥password</code> (URL и числа выбрасываются)</li>
        </ul>
        <p class="mt-1">Дубликаты по (site, login) не задвоятся в пуле.</p>
      </div>
      <form onsubmit={submitCreate} class="mt-4 space-y-3">
        <div>
          <label for="b_name" class="block text-xs font-medium text-slate-700">Name *</label>
          <input id="b_name" type="text" bind:value={newName} required
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="b_file" class="block text-xs font-medium text-slate-700">File * (.csv or .txt)</label>
          <input id="b_file" type="file" accept=".csv,.txt" required
                 onchange={(e) => { newFile = (e.currentTarget as HTMLInputElement).files?.[0] ?? null }}
                 class="mt-1 w-full text-sm" />
          {#if newFile}<p class="mt-1 text-[11px] text-slate-400">{newFile.name} · {(newFile.size / 1024).toFixed(1)} KB</p>{/if}
        </div>
        {#if isSuper}
        <div>
          <label for="b_tag" class="block text-xs font-medium text-slate-700">Tag</label>
          <input id="b_tag" type="text" bind:value={newTag}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        {/if}
        <div>
          <label for="b_note" class="block text-xs font-medium text-slate-700">Note</label>
          <textarea id="b_note" bind:value={newNote} rows="2"
                    placeholder="Откуда: фрилансер с kwork, контакт @nick, ..."
                    class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm"></textarea>
        </div>
        {#if isSuper}
        <div class="grid grid-cols-3 gap-2">
          <div class="col-span-2">
            <label for="b_cost" class="block text-xs font-medium text-slate-700">Cost (optional)</label>
            <input id="b_cost" type="number" step="0.01" bind:value={newCost}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
          <div>
            <label for="b_cur" class="block text-xs font-medium text-slate-700">Currency</label>
            <input id="b_cur" type="text" bind:value={newCurrency} maxlength="6"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
        </div>
        {/if}
        <p class="rounded-md border border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-500">
          После загрузки запусти проверку кнопкой <b>Validate</b> на странице батча — всегда полный цикл (XML-RPC → admin-вход, включая обход Cloudflare).
        </p>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" disabled={createBusy || !newFile}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {createBusy ? 'Uploading…' : 'Upload'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Validate modal -->
{#if validateFor}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (validateFor = null)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Validate batch #{validateFor.id}</h2>
      <p class="mt-1 text-xs text-slate-500">{validateFor.name} · {validateFor.total_credentials} credentials</p>
      <form onsubmit={submitValidate} class="mt-4 space-y-3">
        <div>
          <label for="v_scope" class="block text-xs font-medium text-slate-700">Scope</label>
          <select id="v_scope" bind:value={vScope}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            <option value="all">All ({validateFor.total_credentials})</option>
            <option value="invalid">Only invalid ({validateFor.invalid_count})</option>
            <option value="pending">Pending (never validated)</option>
          </select>
        </div>
        <div>
          <label for="v_conc" class="block text-xs font-medium text-slate-700">Concurrency</label>
          <input id="v_conc" type="number" bind:value={vConcurrency} min="1" max="50"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <p class="mt-1 text-[11px] text-slate-400">Default 5. Больше = быстрее, но больше шанс на CF-блокировку.</p>
        </div>
        <div>
          <label for="v_proxy" class="block text-xs font-medium text-slate-700">Proxy (recommended)</label>
          <select id="v_proxy" bind:value={vProxyId}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            <option value={null}>— весь пул (ротация + ретрай дохлых) —</option>
          </select>
        </div>
        <div>
          <label class="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" bind:checked={vDetectLang} class="rounded border-slate-300" />
            Detect site language (+1 GET per site)
          </label>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (validateFor = null)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" disabled={validateBusy}
                  class="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:bg-slate-300">
            {#if validateBusy}Starting…{:else}<Play size={14} class="inline-block align-text-bottom" /> Start validation{/if}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  /* Анимированные stripes — индикатор "идёт валидация" в pending-зоне бара. */
  .bar-stripes-mini {
    background-image: linear-gradient(
      45deg,
      rgba(99, 102, 241, 0.5) 25%,
      rgba(99, 102, 241, 0.15) 25%,
      rgba(99, 102, 241, 0.15) 50%,
      rgba(99, 102, 241, 0.5) 50%,
      rgba(99, 102, 241, 0.5) 75%,
      rgba(99, 102, 241, 0.15) 75%,
      rgba(99, 102, 241, 0.15)
    );
    background-size: 10px 10px;
    animation: bar-stripes-mini 1s linear infinite;
  }
  @keyframes bar-stripes-mini {
    from { background-position: 0 0; }
    to   { background-position: 10px 0; }
  }
</style>
