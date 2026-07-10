<script lang="ts">
  import { AlertTriangle, ArrowLeft, ArrowRight, Check, ChevronDown, ChevronRight, Copy, Download, Eye, EyeOff, Pause, Play, RotateCcw, RotateCw, UserPlus, X } from 'lucide-svelte'
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { onDestroy, onMount } from 'svelte'

  import { proxies as proxiesApi, wpBatches as batchesApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import RoleLegend from '$lib/components/ui/RoleLegend.svelte'
  import type { BatchCredEntry, Proxy, WpBatch, WpBatchStatus } from '$lib/api/types'
  import { copyText } from '$lib/clipboard'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let batchId = $derived(Number(page.params.id))

  let batch = $state<WpBatch | null>(null)
  let creds = $state<BatchCredEntry[]>([])
  let proxiesList = $state<Proxy[]>([])
  let loading = $state(true)
  let credsLoading = $state(false)
  let hasMore = $state(false)
  let pollTimer: ReturnType<typeof setInterval> | null = null

  // Filters
  let filterStatus = $state<'all' | 'valid' | 'invalid' | 'transient' | 'pending' | 'duplicates'>('all')
  let search = $state('')

  let isSuper = $derived($currentUser?.is_super_admin ?? false)
  // Владелец батча (поставщик своего батча) — может видеть свои пароли.
  let isOwner = $derived(!!batch && batch.created_by_user_id === ($currentUser?.id ?? -1))
  let exportOpen = $state(false)

  // Раскрываемые строки таблицы (детали: login/password/uses + copy).
  // Login/password/uses нужны редко, прячем по умолчанию чтобы освободить
  // место под status / outcome / channels. В экспортах эти поля остаются.
  let expandedRows = $state<Set<number>>(new Set())
  function toggleExpand(credId: number) {
    if (expandedRows.has(credId)) expandedRows.delete(credId)
    else expandedRows.add(credId)
    expandedRows = new Set(expandedRows) // trigger reactivity on Set mutation
  }
  let exportBase = $derived(`/admin/api/batches/${batchId}/export`)
  let exportQuery = $derived(filterStatus !== 'all' ? `?status=${filterStatus}` : '')
  let exportSuffix = $derived(filterStatus !== 'all' ? `-${filterStatus}` : '')
  const EXPORT_FORMATS = [
    { ext: 'csv', label: 'CSV' },
    { ext: 'xlsx', label: 'Excel (XLSX)' },
    { ext: 'txt', label: 'TXT (domain⇥url⇥login⇥pw)' },
    { ext: 'json', label: 'JSON' },
  ] as const

  // Закрыть dropdown при клике вне него (Svelte action).
  function clickOutside(node: HTMLElement, callback: () => void) {
    const handler = (e: MouseEvent) => {
      if (!node.contains(e.target as Node)) callback()
    }
    document.addEventListener('click', handler, true)
    return { destroy() { document.removeEventListener('click', handler, true) } }
  }

  // Tick (1s) для elapsed / ETA пересчётa. Идёт только когда статус
  // validating — иначе пустая трата CPU.
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

  let elapsedMs = $derived(
    batch?.validation_started_at
      ? nowMs - new Date(batch.validation_started_at).getTime()
      : 0
  )
  let doneSoFar = $derived(
    (batch?.valid_count ?? 0) + (batch?.invalid_count ?? 0) + (batch?.transient_count ?? 0)
  )
  let ratePerSec = $derived(
    elapsedMs > 1000 && doneSoFar > 0 ? doneSoFar / (elapsedMs / 1000) : 0
  )
  let etaMs = $derived(
    batch && ratePerSec > 0
      ? Math.max(0, ((batch.total_credentials ?? 0) - doneSoFar) / ratePerSec) * 1000
      : Infinity
  )

  // Password reveal — per-cred toggle. Сами пароли уже в DOM (для super_admin
  // — приходят в API). UI скрывает «••••••» пока не кликнут показать.
  let revealedCredIds = $state<Set<number>>(new Set())
  function toggleReveal(credId: number) {
    if (revealedCredIds.has(credId)) revealedCredIds.delete(credId)
    else revealedCredIds.add(credId)
    revealedCredIds = new Set(revealedCredIds)  // trigger reactivity
  }
  async function copyToClipboard(text: string, label: string) {
    if (await copyText(text)) showToast('success', `${label} copied`)
    else showToast('error', 'Clipboard access denied')
  }

  async function loadBatch() {
    try {
      batch = await batchesApi.get(batchId)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // Token-based race protection: каждый loadCreds увеличивает token, и
  // только последний по timeline присвоит creds. Предыдущие в-flight
  // запросы отбрасывают результат. Иначе polling (3s) перетирает
  // только что выбранный пользователем фильтр.
  let credsReqToken = 0
  async function loadCreds(reset = true) {
    const token = ++credsReqToken
    credsLoading = true
    const sentStatus = filterStatus === 'all' ? undefined : filterStatus
    const sentSearch = search || undefined
    try {
      const res = await batchesApi.credentials(batchId, {
        status: sentStatus,
        search: sentSearch,
        limit: 200,
        after_id: reset ? undefined : creds[creds.length - 1]?.id,
        include_password: isSuper || isOwner,
      })
      // Игнорируем устаревший ответ: stale request finished after a newer one.
      if (token !== credsReqToken) {
        console.debug('[batch-detail] loadCreds stale, drop', { token, latest: credsReqToken, sentStatus })
        return
      }
      // Дополнительная защита: если filterStatus / search изменились во
      // время запроса (e.g. пользователь нажал на другой filter) —
      // ответ может быть от старого filter, отбрасываем.
      const currentStatus = filterStatus === 'all' ? undefined : filterStatus
      const currentSearch = search || undefined
      if (currentStatus !== sentStatus || currentSearch !== sentSearch) {
        console.debug('[batch-detail] loadCreds filter changed mid-flight, drop', { sentStatus, currentStatus })
        return
      }
      console.debug('[batch-detail] loadCreds got', res.items.length, 'items (filter=', sentStatus, ')')
      creds = reset ? res.items : [...creds, ...res.items]
      hasMore = res.has_more
    } catch (e) {
      if (token === credsReqToken) {
        showToast('error', e instanceof ApiError ? e.message : String(e))
      }
    } finally {
      if (token === credsReqToken) credsLoading = false
    }
  }
  // Helper: set filter и сразу перезагружаем — обходим возможные гонки
  // между $state setter и читателем внутри loadCreds.
  async function pickFilter(s: 'all' | 'valid' | 'invalid' | 'transient' | 'pending' | 'duplicates') {
    filterStatus = s
    await loadCreds(true)
    document.getElementById('creds-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  async function loadProxies() {
    try {
      const r = await proxiesApi.list({ status: 'active', limit: 200 })
      proxiesList = r.items
    } catch { proxiesList = [] }
  }

  async function refresh() {
    await loadBatch()
    await loadCreds(true)
  }
  // Polling-friendly refresh: только batch counters/status (для прогресс-бара),
  // НЕ перетирает таблицу creds. Иначе при выбранном фильтре polling каждые
  // 3с перезагружает таблицу с тем же фильтром, но создаёт гонки и
  // «мерцание» строк. Таблица обновляется только: при mount, клике на
  // фильтр, Load more, и операциях типа force-status.
  async function refreshBatchOnly() {
    await loadBatch()
  }

  onMount(async () => {
    loading = true
    await refresh()
    loading = false
    // Polling: каждые 3 сек если идёт валидация (агрессивно — UI live);
    // каждые 15 сек для "spectator" статусов (uploaded/ready/paused/done) —
    // чтобы заметить переход в validating если кто-то стартанул её снаружи
    // (CLI / другой пользователь / cron). Cheap по бэку, важно для UX.
    let tickCounter = 0
    pollTimer = setInterval(() => {
      tickCounter += 1
      const isValidating = batch?.status === 'validating'
      // Каждый тик (3с) если validating; каждые 5 тиков (15с) иначе.
      // refreshBatchOnly: НЕ перетирает таблицу creds (иначе ломает текущий фильтр).
      if (isValidating || tickCounter % 5 === 0) refreshBatchOnly()
    }, 3000)
    // 1-sec tick для elapsed / ETA
    etaTimer = setInterval(() => { nowMs = Date.now() }, 1000)
  })
  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer)
    if (etaTimer) clearInterval(etaTimer)
  })

  function statusBadge(s: WpBatchStatus): string {
    switch (s) {
      case 'uploaded': return 'bg-slate-100 text-slate-600'
      case 'queued': return 'bg-indigo-100 text-indigo-700'
      case 'validating': return 'bg-brand-100 text-brand-700'
      case 'paused': return 'bg-amber-100 text-amber-700'
      case 'done': return 'bg-emerald-100 text-emerald-700'
      default: return 'bg-slate-100 text-slate-500'
    }
  }
  function fmt(d: string | null): string { return d ? new Date(d).toLocaleString() : '—' }

  function progressPct(b: WpBatch): number {
    if (b.total_credentials === 0) return 0
    const done = b.valid_count + b.invalid_count + b.transient_count
    return Math.round((done / b.total_credentials) * 100)
  }
  function pct(n: number, total: number): number {
    if (!total) return 0
    return Math.round((n * 100) / total)
  }

  // ─── Actions ───────────────────────────────────────────────────────

  let validateOpen = $state(false)
  let vScope = $state<'all' | 'invalid' | 'pending'>('all')
  let vConcurrency = $state<number | null>(null)  // пусто = серверный дефолт
  let vProxyId = $state<number | null>(null)
  let vDetectLang = $state(true)
  let vLevel = $state<'light' | 'medium' | 'full'>('full')   // по умолчанию полный цикл
  let vProvision = $state(true)
  let vBusy = $state(false)

  // Provision (создание наших author-аккаунтов) для всего батча
  let provBusy = $state(false)

  // Reset validation (danger zone) — требует ввода имени батча
  let resetOpen = $state(false)
  let resetConfirm = $state('')
  let resetBusy = $state(false)

  async function submitValidate(e: SubmitEvent) {
    e.preventDefault()
    if (!batch) return
    vBusy = true
    try {
      await batchesApi.validate(batchId, {
        scope: vScope,
        concurrency: vConcurrency ?? undefined,
        proxy_id: vProxyId,
        detect_language: vDetectLang,
        level: vLevel,
        provision_after: vProvision,
        provision_role: 'author',
      })
      showToast('success', `Validation started (${vScope}, ${vLevel})${vProvision ? ' + provision' : ''}`)
      validateOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { vBusy = false }
  }

  async function doProvision() {
    if (provBusy) return
    let n = 0
    try {
      const c = await batchesApi.provisionCount(batchId)
      n = c.provisionable
    } catch { /* ignore preview error */ }
    const msg = n > 0
      ? `Создать наши author-аккаунты на ${n} сайтах батча (где есть admin-доступ и нашего аккаунта ещё нет)?`
      : 'Нет подходящих сайтов: нужен валидный admin-доступ с правом create_users и отсутствие нашего аккаунта. Запустить всё равно?'
    if (!confirm(msg)) return
    provBusy = true
    try {
      const r = await batchesApi.provision(batchId, 'author')
      showToast('success', `Provision запущен для ${r.provisionable} сайтов (фоном)`)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { provBusy = false }
  }

  async function doPause() {
    try { await batchesApi.pause(batchId); showToast('success', 'Pause requested'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
  async function doResume() {
    try { await batchesApi.resume(batchId); showToast('success', 'Resumed'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
  async function doRevalidateFailed() {
    try { await batchesApi.revalidateFailed(batchId); showToast('success', 'Re-validation queued'); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
  async function doDelete() {
    if (!batch) return
    if (!confirm(`Удалить batch "${batch.name}"? Credentials в пуле останутся.`)) return
    try {
      await batchesApi.remove(batchId)
      showToast('success', 'Deleted')
      goto('/batches')
    } catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }

  // Имя введено точно? (включает кнопку подтверждения сброса)
  let resetNameOk = $derived(!!batch && resetConfirm.trim() === batch.name)
  async function doResetValidation() {
    if (!batch || resetBusy || !resetNameOk) return
    resetBusy = true
    try {
      const r = await batchesApi.resetValidation(batchId, resetConfirm.trim())
      showToast('success', `Валидация сброшена: ${r.creds_reset} creds → pending`)
      resetOpen = false
      resetConfirm = ''
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { resetBusy = false }
  }

  // WP-роль → короткий бейдж с цветом. administrator выделен (важно для
  // provision-author / сквозных ссылок — только админ может).
  function roleBadge(role: string | null | undefined): { label: string; cls: string } {
    switch (role) {
      case 'administrator': return { label: 'admin', cls: 'bg-purple-100 text-purple-700' }
      case 'editor':        return { label: 'editor', cls: 'bg-blue-100 text-blue-700' }
      case 'author':        return { label: 'author', cls: 'bg-emerald-100 text-emerald-700' }
      case 'contributor':   return { label: 'contrib', cls: 'bg-amber-100 text-amber-700' }
      case 'subscriber':    return { label: 'subscr', cls: 'bg-slate-100 text-slate-500' }
      default:              return { label: role || '?', cls: 'bg-slate-100 text-slate-600' }
    }
  }

  function credStatusLabel(c: BatchCredEntry): { label: string; cls: string; title: string } {
    if (c.last_validation_kind === 'duplicate') {
      return { label: 'дубликат', cls: 'bg-slate-100 text-slate-500', title: 'Этот доступ уже был загружен ранее (в системе уже есть)' }
    }
    if (c.last_validated_at === null) {
      return { label: 'pending', cls: 'bg-slate-100 text-slate-500', title: 'Никогда не валидировался' }
    }
    const kind = c.last_validation_kind
    // Manual overrides — приоритет над всем
    if (kind === 'manual_valid') {
      return { label: 'forced valid', cls: 'bg-emerald-100 text-emerald-700', title: 'Помечен валидным вручную' }
    }
    if (kind === 'manual_invalid') {
      return { label: 'forced invalid', cls: 'bg-red-100 text-red-700', title: 'Помечен невалидным вручную' }
    }
    // is_valid — финальный verdict (учитывает Tier 1 + Tier 2). Колонка Outcome
    // отдельно показывает «через что» прошёл — RPC или admin. Здесь только статус.
    if (c.is_valid) {
      if (kind === 'ok') {
        return { label: 'valid', cls: 'bg-emerald-100 text-emerald-700', title: 'XML-RPC: ответил успехом' }
      }
      if (c.can_admin_login === true) {
        return {
          label: 'valid · admin',
          cls: 'bg-emerald-100 text-emerald-700',
          title: `Admin login прошёл (XML-RPC: ${kind || 'не работает'})`,
        }
      }
      // Legacy: pre-0018 кред без kind, но is_valid=true
      if (!kind) {
        return { label: 'valid', cls: 'bg-emerald-100 text-emerald-700', title: 'Validated (legacy — без detail kind)' }
      }
      return { label: 'valid', cls: 'bg-emerald-100 text-emerald-700', title: kind }
    }
    // is_valid=false — invalid с конкретной причиной
    if (kind === 'auth_invalid' || kind === 'permission_denied') {
      return { label: 'auth fail', cls: 'bg-red-100 text-red-700', title: c.last_error_message || 'Wrong username / password' }
    }
    if (kind === 'site_disabled') {
      return { label: 'site dead', cls: 'bg-red-100 text-red-700', title: 'Сайт авто-выключен после серии fail-ов' }
    }
    if (kind === 'parked') {
      return { label: 'parked', cls: 'bg-red-100 text-red-700', title: c.last_error_message || 'Домен — parking-страница, suspended account, либо default-page хостера' }
    }
    // is_valid=false но Tier 1 inconclusive и Tier 2 не подтвердил → not_confirmed
    return {
      label: 'not confirmed',
      cls: 'bg-red-100 text-red-700',
      title: c.last_error_message || `Не подтверждён ни одним каналом (${kind || 'unknown'})`,
    }
  }

  async function forceCredStatus(c: BatchCredEntry, makeValid: boolean) {
    const verb = makeValid ? 'Mark VALID' : 'Mark INVALID'
    if (!confirm(`${verb} cred #${c.id} (${c.domain} / ${c.login})?\n\nЭто override — перевалидация не запускается.`)) return
    try {
      await batchesApi.forceCredStatus(batchId, c.id, makeValid)
      showToast('success', `${verb} → #${c.id}`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
</script>

<div class="space-y-5">
  <div>
    <a href="/batches" class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Batches</a>
    {#if loading}
      <h1 class="mt-1 text-2xl font-semibold text-slate-900">Loading…</h1>
    {:else if batch}
      <div class="mt-1 flex flex-wrap items-center gap-3">
        <h1 class="text-2xl font-semibold text-slate-900">{batch.name}</h1>
        <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusBadge(batch.status)}">
          {batch.status}{#if batch.pause_requested && batch.status === 'validating'} (pausing){/if}
        </span>
      </div>
      <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
        {#if batch.source_filename}<span>File: <code class="rounded bg-slate-100 px-1">{batch.source_filename}</code></span>{/if}
        {#if batch.tag}<span>Tag: <strong>{batch.tag}</strong></span>{/if}
        {#if batch.cost_total != null}
          <span>Cost: <strong>{batch.cost_total} {batch.cost_currency ?? ''}</strong>
            {#if batch.valid_count > 0}<span class="text-slate-400">· {(batch.cost_total / batch.valid_count).toFixed(3)} per valid</span>{/if}
          </span>
        {/if}
        <span>Created: {fmt(batch.created_at)}</span>
        {#if batch.validation_started_at}<span>Started: {fmt(batch.validation_started_at)}</span>{/if}
        {#if batch.validation_finished_at}<span>Finished: {fmt(batch.validation_finished_at)}</span>{/if}
      </div>
      {#if batch.note}
        <p class="mt-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">{batch.note}</p>
      {/if}
    {/if}
  </div>

  {#if batch}
    <div class="flex flex-wrap gap-2">
      {#if batch.status === 'uploaded' || batch.status === 'done'}
        <button onclick={() => (validateOpen = true)}
                class="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700">
          <Play size={14} class="inline-block align-text-bottom" /> Validate
        </button>
      {/if}
      {#if batch.status === 'validating' && !batch.pause_requested}
        <button onclick={doPause}
                class="rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-100">
          <Pause size={14} class="inline-block align-text-bottom" /> Pause
        </button>
      {/if}
      {#if batch.status === 'paused' || (batch.status === 'validating' && batch.pause_requested)}
        <button onclick={doResume}
                class="rounded-md border border-brand-300 bg-brand-50 px-3 py-1.5 text-sm font-medium text-brand-800 hover:bg-brand-100">
          <Play size={14} class="inline-block align-text-bottom" /> {batch.status === 'paused' ? 'Resume' : 'Resume (перезапустить)'}
        </button>
      {/if}
      {#if batch.status === 'done' && batch.invalid_count > 0}
        <button onclick={doRevalidateFailed}
                class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          <RotateCw size={14} class="inline-block align-text-bottom" /> Re-validate failed ({batch.invalid_count})
        </button>
      {/if}
      {#if isSuper && (batch.status === 'uploaded' || batch.status === 'done')}
        <button onclick={doProvision} disabled={provBusy} title="Создать наши author-аккаунты на admin-сайтах батча, где их ещё нет"
                class="rounded-md border border-blue-300 bg-blue-50 px-3 py-1.5 text-sm font-medium text-blue-800 hover:bg-blue-100 disabled:opacity-50">
          <UserPlus size={14} class="inline-block align-text-bottom" /> {provBusy ? 'Provisioning…' : 'Provision наши аккаунты'}
        </button>
      {/if}
      <!-- Export dropdown: 4 формата × current filter (то что в таблице).
           Super_admin only · с паролями · audit-logged. -->
      <div class="relative ml-auto" use:clickOutside={() => (exportOpen = false)}>
        <button type="button" onclick={() => (exportOpen = !exportOpen)}
                class="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
                title={filterStatus === 'all'
                  ? 'Export all credentials (with passwords). Super_admin only · audit-logged.'
                  : `Export only "${filterStatus}" — what you see in the table now.`}>
          <Download size={14} /> Export
          {#if filterStatus !== 'all'}
            <span class="rounded bg-slate-100 px-1.5 text-[10px]">{filterStatus}</span>
          {/if}
          <span class="text-slate-400">▾</span>
        </button>
        {#if exportOpen}
          <div class="absolute right-0 z-10 mt-1 w-48 overflow-hidden rounded-md border border-slate-200 bg-white shadow-lg">
            {#each EXPORT_FORMATS as f}
              <a href="{exportBase}.{f.ext}{exportQuery}"
                 download="batch-{batchId}{exportSuffix}.{f.ext}"
                 onclick={() => (exportOpen = false)}
                 class="block px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
                {f.label}
              </a>
            {/each}
          </div>
        {/if}
      </div>
      {#if isSuper && batch.status !== 'validating'}
        <button onclick={() => { resetConfirm = ''; resetOpen = true }}
                title="Сбросить всю валидацию батча — все creds вернутся в pending для чистого повторного прогона"
                class="rounded-md border border-red-500 bg-red-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-700">
          <RotateCcw size={14} class="inline-block align-text-bottom" /> Сбросить валидацию
        </button>
      {/if}
      <button onclick={doDelete}
              class="rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50">
        <X size={14} class="inline-block align-text-bottom" /> Delete
      </button>
    </div>
  {/if}

  <!-- Counters card -->
  {#if batch}
    {@const total = batch.total_credentials}
    {@const dups = batch.duplicate_credentials ?? 0}
    {@const validated = batch.valid_count + batch.invalid_count + batch.transient_count}
    {@const validPct = pct(batch.valid_count, total)}
    {@const invalidPct = pct(batch.invalid_count, total)}
    {@const transientPct = pct(batch.transient_count, total)}
    <!-- duplicates пропускаются валидатором (другой cred этого сайта уже идёт);
         считаем как processed — иначе финиш застревает на 97% при 3 dup -->
    {@const done = validated + dups}
    {@const doneCount = done}
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center justify-between gap-3">
        <div class="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-700">
          <span>Validation progress</span>
          {#if batch.status === 'validating'}
            <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-brand-600"></span>
            <span class="text-xs text-brand-700">{doneCount} / {total} processed</span>
            <span class="text-xs text-slate-400">·</span>
            <span class="text-xs text-slate-600" title="Elapsed since validation start">
              {fmtDuration(elapsedMs)} elapsed
            </span>
            {#if isFinite(etaMs) && etaMs > 0 && doneCount < total}
              <span class="text-xs text-slate-400">·</span>
              <span class="text-xs text-slate-600" title="Estimated time remaining">
                ~{fmtDuration(etaMs)} left
              </span>
            {/if}
            {#if ratePerSec > 0}
              <span class="text-xs text-slate-400">·</span>
              <span class="text-xs text-slate-500" title="Throughput">
                {ratePerSec >= 1
                  ? `${ratePerSec.toFixed(1)} cred/s`
                  : `${(60 * ratePerSec).toFixed(1)} cred/min`}
              </span>
            {/if}
          {:else if batch.status === 'paused'}
            <span class="text-xs text-amber-700">paused at {doneCount} / {total}</span>
          {:else if batch.validation_finished_at}
            <span class="text-xs text-slate-500">{doneCount} / {total} processed</span>
            {#if batch.validation_started_at}
              <span class="text-xs text-slate-400">·</span>
              <span class="text-xs text-slate-500" title="Total validation duration">
                took {fmtDuration(new Date(batch.validation_finished_at).getTime() - new Date(batch.validation_started_at).getTime())}
              </span>
            {/if}
          {/if}
        </div>
        <span class="text-sm font-semibold text-slate-700">{pct(done, total)}%</span>
      </div>
      <!-- Segmented bar: green / red / amber stack. Pending track:
           если идёт валидация — animated stripes (живой "ползущий" фон),
           иначе пустая серая полоска. -->
      <div class="mt-3 flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div class="h-full bg-emerald-500 transition-all" style="width: {validPct}%"></div>
        <div class="h-full bg-red-500 transition-all" style="width: {invalidPct}%"></div>
        <div class="h-full bg-amber-400 transition-all" style="width: {transientPct}%"></div>
        {#if dups > 0}
          {@const dupsPct = total > 0 ? Math.round((dups / total) * 100) : 0}
          <div class="h-full bg-slate-400 transition-all" style="width: {dupsPct}%" title="Duplicates skipped"></div>
        {/if}
        {#if batch.status === 'validating' && !batch.pause_requested}
          {@const pendingPct = Math.max(0, 100 - validPct - invalidPct - transientPct - (total > 0 ? Math.round((dups / total) * 100) : 0))}
          {#if pendingPct > 0}
            <div
              class="h-full bar-stripes"
              style="width: {pendingPct}%"
              title="In progress…"
            ></div>
          {/if}
        {/if}
      </div>
      <!-- Stat cards: кликабельные фильтры. Active card → bg highlight. -->
      <div class="mt-4 grid grid-cols-2 gap-4 text-center sm:grid-cols-7">
        <button type="button" onclick={() => pickFilter('all')}
                class="rounded-md p-1 transition hover:bg-slate-50 {filterStatus === 'all' ? 'bg-slate-100 ring-1 ring-slate-300' : ''}">
          <div class="text-2xl font-semibold text-slate-900">{total}</div>
          <div class="text-[11px] uppercase tracking-wider text-slate-500">Total</div>
        </button>
        <button type="button" onclick={() => pickFilter('valid')}
                class="rounded-md p-1 transition hover:bg-emerald-50 {filterStatus === 'valid' ? 'bg-emerald-50 ring-1 ring-emerald-300' : ''}">
          <div class="text-2xl font-semibold text-emerald-600">{batch.valid_count}</div>
          <div class="text-[11px] uppercase tracking-wider text-slate-500">
            Valid{#if total > 0} · {validPct}%{/if}
          </div>
          {#if (batch.valid_xmlrpc_count ?? 0) + (batch.valid_admin_count ?? 0) > 0}
            <div class="mt-0.5 text-[10px] text-slate-500" title="Tier 1 RPC vs Tier 2 admin login">
              <span class="text-emerald-700">{batch.valid_xmlrpc_count ?? 0}</span> rpc
              · <span class="text-emerald-700">{batch.valid_admin_count ?? 0}</span> admin
            </div>
          {/if}
        </button>
        <button type="button" onclick={() => pickFilter('invalid')}
                class="rounded-md p-1 transition hover:bg-red-50 {filterStatus === 'invalid' ? 'bg-red-50 ring-1 ring-red-300' : ''}">
          <div class="text-2xl font-semibold text-red-600">{batch.invalid_count}</div>
          <div class="text-[11px] uppercase tracking-wider text-slate-500">
            Invalid{#if total > 0} · {invalidPct}%{/if}
          </div>
        </button>
        <button type="button" onclick={() => pickFilter('transient')}
                class="rounded-md p-1 transition hover:bg-amber-50 {filterStatus === 'transient' ? 'bg-amber-50 ring-1 ring-amber-300' : ''}">
          <div class="text-2xl font-semibold text-amber-600">{batch.transient_count}</div>
          <div class="text-[11px] uppercase tracking-wider text-slate-500">
            Transient{#if total > 0} · {transientPct}%{/if}
          </div>
        </button>
        <button type="button" onclick={() => pickFilter('pending')}
                class="rounded-md p-1 transition hover:bg-brand-50 {filterStatus === 'pending' ? 'bg-brand-50 ring-1 ring-brand-300' : ''}">
          <div class="text-2xl font-semibold text-brand-700">{batch.pending_count}</div>
          <div class="text-[11px] uppercase tracking-wider text-slate-500">Pending</div>
        </button>
        {#if isSuper}
          <button type="button" onclick={() => pickFilter('duplicates')}
                  title="Show the original credentials (in their other batches) that this import skipped as duplicates"
                  class="rounded-md p-1 transition hover:bg-slate-100 {filterStatus === 'duplicates' ? 'bg-slate-100 ring-1 ring-slate-300' : ''}">
            <div class="text-2xl font-semibold text-slate-500">{dups}</div>
            <div class="text-[11px] uppercase tracking-wider text-slate-500">Duplicates</div>
          </button>
          <div class="rounded-md p-1" title="Кредов, на которых создан наш author-аккаунт (provision)">
            <div class="text-2xl font-semibold text-blue-600">{batch.provisioned_count ?? 0}</div>
            <div class="text-[11px] uppercase tracking-wider text-slate-500">Наши аккаунты</div>
          </div>
        {:else}
          <!-- supplier: дубликаты-фильтр показывает только domain+login (его же
               данные), без паролей/статуса оригинала (см. бэкенд). -->
          <button type="button" onclick={() => pickFilter('duplicates')}
                  title="Доступы, которые уже были в системе (загружены ранее)"
                  class="rounded-md p-1 transition hover:bg-slate-100 {filterStatus === 'duplicates' ? 'bg-slate-100 ring-1 ring-slate-300' : ''}">
            <div class="text-2xl font-semibold text-slate-500">{dups}</div>
            <div class="text-[11px] uppercase tracking-wider text-slate-500">Дубликаты</div>
          </button>
        {/if}
      </div>
    </div>
  {/if}

  <!-- Credentials table -->
  <section id="creds-section">
    <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
      <h2 class="text-lg font-medium text-slate-900">
        Credentials
        <span class="ml-2 text-sm font-normal text-slate-500">
          {creds.length}{#if filterStatus !== 'all'} · filter: <span class="font-mono">{filterStatus}</span>{/if}{#if credsLoading} · loading…{/if}
        </span>
      </h2>
      <div class="flex flex-wrap items-center gap-2">
        <input type="search" bind:value={search}
               placeholder="search domain / login"
               onchange={() => loadCreds(true)}
               class="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        <select bind:value={filterStatus} onchange={() => loadCreds(true)}
                class="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="all">All</option>
          <option value="valid">Valid only</option>
          <option value="invalid">Invalid only</option>
          <option value="transient">Transient (not confirmed)</option>
          <option value="pending">Pending (never)</option>
          <option value="duplicates">Duplicates (in other batches)</option>
        </select>
      </div>
    </div>

    {#if creds.length === 0}
      <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">
        {credsLoading ? 'Loading…' : 'No credentials in this filter.'}
      </div>
    {:else}
      <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
            <tr>
              <th class="px-3 py-2 w-8"></th><!-- expand chevron -->
              <th class="px-3 py-2">ID</th>
              <th class="px-3 py-2">Domain</th>
              <th class="px-3 py-2">Lang</th>
              <th class="px-3 py-2">Status</th>
              <th class="px-3 py-2 text-center" title="Capability matrix: XML-RPC channel (✓ ok, ✕ not working, — not yet checked) · admin form-login channel">Channels</th>
              <th class="px-3 py-2 text-center" title="WP-роль пользователя доступа (XML-RPC wp.getProfile / админ-меню)">
                <span class="inline-flex items-center gap-1">Role<RoleLegend align="left" /></span>
              </th>
              <th class="px-3 py-2">Outcome / Error</th>
              <th class="px-3 py-2">Last validated</th>
              {#if isSuper}<th class="px-3 py-2 text-right">Force</th>{/if}
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            {#each creds as c (c.id)}
              {@const s = credStatusLabel(c)}
              {@const isExpanded = expandedRows.has(c.id)}
              <tr class="align-top" class:bg-blue-50={c.provisioned} class:hover:bg-blue-100={c.provisioned}
                  class:hover:bg-slate-50={!c.provisioned}>
                <td class="px-1 py-2 text-center">
                  <button onclick={() => toggleExpand(c.id)}
                          title={isExpanded ? 'Hide details' : 'Show login / password / uses'}
                          class="text-slate-400 hover:text-slate-700">
                    {#if isExpanded}<ChevronDown size={14} />{:else}<ChevronRight size={14} />{/if}
                  </button>
                </td>
                <td class="px-3 py-2 text-slate-500">{c.id}</td>
                <td class="px-3 py-2 font-medium text-slate-900">
                  <a href={`/wp-sites/${c.site_id}`} class="hover:text-brand-600 hover:underline">{c.domain}</a>
                  {#if c.provisioned}
                    <span class="ml-1 inline-flex items-center gap-0.5 rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700"
                          title={`Создан нами (provision-author${c.provisioned_via ? ', через ' + c.provisioned_via : ''}${c.provisioned_at ? ', ' + fmt(c.provisioned_at) : ''})`}>
                      <UserPlus size={10} /> наш
                    </span>
                  {:else if c.provisioned_here}
                    <span class="ml-1 inline-flex items-center gap-0.5 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700"
                          title="Через этот admin-доступ мы создали наш author-аккаунт на сайте (см. /wp-sites)">
                      <UserPlus size={10} /> создан наш ✓
                    </span>
                  {/if}
                  {#if filterStatus === 'duplicates' && c.import_batch_id && c.import_batch_id !== batchId}
                    <a href={`/batches/${c.import_batch_id}`}
                       class="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-200"
                       title="Original batch">from #{c.import_batch_id}</a>
                  {/if}
                </td>
                <td class="px-3 py-2 text-xs">
                  {#if c.language}
                    <span class="rounded-md bg-slate-100 px-1.5 py-0.5 uppercase">{c.language}</span>
                  {:else if c.language_detected_at}
                    <span class="rounded-md bg-slate-50 px-1.5 py-0.5 text-slate-400 italic"
                          title="Lang detect был запущен, но язык не определён (пустая SPA-страница, mojibake, или слишком мало текста)">
                      none
                    </span>
                  {:else}<span class="text-slate-400" title="Не пытались определить">—</span>{/if}
                </td>
                <td class="px-3 py-2">
                  <span title={s.title}
                        class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {s.cls}">{s.label}</span>
                </td>
                <td class="px-3 py-2 text-center">
                  <!-- XML-RPC + admin channel indicators. ✓=логин работает, ⚠=эндпоинт жив но логин не прошёл, ✕=отключён, —=не проверяли -->
                  <div class="flex items-center justify-center gap-1.5 text-[11px]">
                    <span class="inline-flex items-center gap-0.5"
                          title={c.can_post_via_xmlrpc === true
                            ? 'XML-RPC: логин работает — можно постить'
                            : c.can_xmlrpc === true
                              ? 'XML-RPC: эндпоинт жив, но логин не прошёл (неверные креды)'
                              : c.can_xmlrpc === false
                                ? 'XML-RPC: отключён / недоступен'
                                : 'XML-RPC: ещё не проверяли'}>
                      <span class="text-slate-400">RPC</span>
                      {#if c.can_post_via_xmlrpc === true}
                        <span class="text-emerald-600">✓</span>
                      {:else if c.can_xmlrpc === true}
                        <span class="text-amber-500">⚠</span>
                      {:else if c.can_xmlrpc === false}
                        <span class="text-red-500">✕</span>
                      {:else}
                        <span class="text-slate-300">—</span>
                      {/if}
                    </span>
                    <span class="text-slate-200">·</span>
                    <span class="inline-flex items-center gap-0.5"
                          title={c.last_admin_check_at
                            ? 'Admin form-login (Tier 2): ' + (c.can_admin_login === true ? 'works' : c.can_admin_login === false ? 'failed' : 'check ran, no decisive answer')
                            : 'Admin channel not checked (run validation with level=medium or full)'}>
                      <span class="text-slate-400">Admin</span>
                      {#if c.can_admin_login === true}
                        <span class="text-emerald-600">✓</span>
                      {:else if c.can_admin_login === false}
                        <span class="text-red-500">✕</span>
                      {:else}
                        <span class="text-slate-300">—</span>
                      {/if}
                    </span>
                  </div>
                </td>
                <td class="px-3 py-2 text-center">
                  {#if c.admin_role}
                    {@const rb = roleBadge(c.admin_role)}
                    <span class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {rb.cls}"
                          title={c.can_create_users ? 'Может создавать пользователей (create_users)' : ''}>
                      {rb.label}{#if c.can_create_users}<span class="ml-0.5" title="create_users">＋</span>{/if}
                    </span>
                  {:else}
                    <span class="text-slate-300">—</span>
                  {/if}
                </td>
                <td class="px-3 py-2 max-w-md">
                  {#if c.last_validation_kind && c.last_validation_kind !== 'ok'}
                    <code class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700">{c.last_validation_kind}</code>
                    {#if c.last_error_message}
                      <div class="mt-0.5 text-[11px] text-red-600 break-all" title={c.last_error_message}>
                        {c.last_error_message.length > 160 ? c.last_error_message.slice(0, 160) + '…' : c.last_error_message}
                      </div>
                    {/if}
                  {:else if c.last_validation_kind === 'ok'}
                    <span class="text-[11px] text-emerald-600">OK</span>
                  {:else}
                    <span class="text-[11px] text-slate-400">—</span>
                  {/if}
                </td>
                <td class="px-3 py-2 text-xs text-slate-500">
                  {fmt(c.last_validated_at)}
                  {#if c.error_counter && c.error_counter > 0}
                    <span class="ml-1 rounded bg-red-50 px-1 text-[10px] text-red-600"
                          title="Consecutive failures (cred auto-invalidates at threshold)">
                      ×{c.error_counter}
                    </span>
                  {/if}
                </td>
                {#if isSuper}
                  <td class="px-3 py-2 text-right whitespace-nowrap">
                    {#if c.is_valid}
                      <button onclick={() => forceCredStatus(c, false)}
                              title="Mark INVALID (override)"
                              class="rounded border border-red-200 px-2 py-0.5 text-[11px] font-medium text-red-700 hover:bg-red-50">
                        <X size={14} class="inline-block align-text-bottom" /> invalid
                      </button>
                    {:else}
                      <button onclick={() => forceCredStatus(c, true)}
                              title="Mark VALID (override)"
                              class="rounded border border-emerald-200 px-2 py-0.5 text-[11px] font-medium text-emerald-700 hover:bg-emerald-50">
                        <Check size={14} class="inline-block align-text-bottom" /> valid
                      </button>
                    {/if}
                  </td>
                {/if}
              </tr>
              {#if isExpanded}
                {@const isRevealed = revealedCredIds.has(c.id)}
                <tr class="bg-slate-50/60">
                  <td></td>
                  <td colspan={isSuper ? 9 : 8} class="px-3 pb-3 pt-1">
                    <dl class="grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-slate-600 sm:grid-cols-3">
                      <div class="flex items-baseline gap-2">
                        <dt class="font-medium text-slate-500 w-16">Login:</dt>
                        <dd class="flex items-center gap-1.5">
                          <span class="select-all font-mono text-slate-800">{c.login}</span>
                          <button onclick={() => copyToClipboard(c.login, 'Login')}
                                  title="Copy login" class="text-slate-400 hover:text-slate-700">
                            <Copy size={11} />
                          </button>
                        </dd>
                      </div>
                      {#if isSuper}
                        <div class="flex items-baseline gap-2">
                          <dt class="font-medium text-slate-500 w-16">Password:</dt>
                          <dd class="flex items-center gap-1.5">
                            {#if c.password}
                              <span class="select-all font-mono text-slate-800">{isRevealed ? c.password : '••••••••'}</span>
                              <button onclick={() => toggleReveal(c.id)}
                                      title={isRevealed ? 'Hide' : 'Show'}
                                      class="text-slate-400 hover:text-slate-700">
                                {#if isRevealed}<EyeOff size={11} />{:else}<Eye size={11} />{/if}
                              </button>
                              <button onclick={() => copyToClipboard(c.password!, 'Password')}
                                      title="Copy password" class="text-slate-400 hover:text-slate-700">
                                <Copy size={11} />
                              </button>
                            {:else}
                              <span class="text-slate-300" title="Не удалось расшифровать">—</span>
                            {/if}
                          </dd>
                        </div>
                      {/if}
                      <div class="flex items-baseline gap-2">
                        <dt class="font-medium text-slate-500 w-16">Uses:</dt>
                        <dd class="text-slate-800">{c.amount_use}</dd>
                      </div>
                    </dl>
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
      {#if hasMore}
        <div class="mt-3 flex justify-center">
          <button onclick={() => loadCreds(false)} disabled={credsLoading}
                  class="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
            {credsLoading ? 'Loading…' : 'Load more'}
          </button>
        </div>
      {/if}
    {/if}
  </section>

  {#if batch?.status === 'validating'}
    <p class="text-xs text-slate-400">Auto-refresh каждые 3 сек.</p>
  {/if}
</div>

<!-- Validate modal -->
{#if validateOpen && batch}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (validateOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Validate batch #{batch.id}</h2>
      <p class="mt-1 text-xs text-slate-500">{batch.name} · {batch.total_credentials} credentials</p>
      <form onsubmit={submitValidate} class="mt-4 space-y-3">
        <div>
          <label for="bv_scope" class="block text-xs font-medium text-slate-700">Scope</label>
          <select id="bv_scope" bind:value={vScope}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            <option value="all">All ({batch.total_credentials})</option>
            <option value="invalid">Only invalid ({batch.invalid_count})</option>
            <option value="pending">Pending (never validated)</option>
          </select>
        </div>
        {#if isSuper}
        <div class="rounded-md border border-emerald-200 bg-emerald-50 p-2 text-[11px] text-emerald-800">
          Полный цикл (всегда): <b>full</b>-валидация (XML-RPC + admin-login + probes)
          <b>и создание наших author-аккаунтов</b> там, где мы админ с правом create_users.
          Отдельной опции нет.
        </div>
        <div>
          <label for="bv_conc" class="block text-xs font-medium text-slate-700">Concurrency <span class="text-slate-400">(пусто = по умолчанию сервера)</span></label>
          <input id="bv_conc" type="number" bind:value={vConcurrency} min="1" max="50" placeholder="сервер"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="bv_proxy" class="block text-xs font-medium text-slate-700">Proxy</label>
          <select id="bv_proxy" bind:value={vProxyId}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            <option value={null}>— весь пул (ротация + ретрай дохлых) —</option>
          </select>
          <p class="mt-1 text-[11px] text-slate-400">
            Round-robin по всему живому пулу с перебором при дохлом прокси (рекомендуется).
          </p>
        </div>
        <div>
          <label class="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" bind:checked={vDetectLang} class="rounded border-slate-300" />
            Detect site language (+1 GET per site)
          </label>
        </div>
        {:else}
          <p class="text-[11px] text-slate-400">
            Проверим каждый доступ (XML-RPC + админ-логин, включая обход Cloudflare). Это займёт несколько минут.
          </p>
        {/if}
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (validateOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" disabled={vBusy}
                  class="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:bg-slate-300">
            {#if vBusy}Starting…{:else}<Play size={14} class="inline-block align-text-bottom" /> Start{/if}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Reset-validation modal (DANGER) -->
{#if resetOpen && batch}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/50 p-4" onclick={() => (resetOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg border-2 border-red-300 bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="flex items-center gap-2 text-lg font-semibold text-red-700">
        <AlertTriangle size={20} /> Сбросить валидацию батча #{batch.id}
      </h2>
      <div class="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
        <p class="font-medium">Это действие необратимо.</p>
        <p class="mt-1 text-[13px]">
          Все <b>{batch.total_credentials}</b> credentials вернутся в статус
          <b>pending</b>: сотрутся вердикты (valid / invalid / transient),
          capability-матрица и cooldown'ы. Логин/пароль и наши provision-аккаунты
          останутся. После сброса запустите валидацию заново (full).
        </p>
      </div>
      <label for="reset_confirm" class="mt-4 block text-xs font-medium text-slate-700">
        Для подтверждения введите имя батча: <span class="font-mono text-red-600">{batch.name}</span>
      </label>
      <input id="reset_confirm" type="text" bind:value={resetConfirm} placeholder={batch.name}
             autocomplete="off"
             class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-red-400 focus:ring-red-400" />
      <div class="mt-4 flex justify-end gap-2">
        <button type="button" onclick={() => (resetOpen = false)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Отмена</button>
        <button type="button" onclick={doResetValidation} disabled={!resetNameOk || resetBusy}
                class="rounded-md bg-red-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:bg-slate-300">
          {#if resetBusy}Сбрасываю…{:else}<RotateCcw size={14} class="inline-block align-text-bottom" /> Сбросить{/if}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Animated stripes для "идёт валидация" pending-track. */
  .bar-stripes {
    background-image: linear-gradient(
      45deg,
      rgba(99, 102, 241, 0.45) 25%,
      rgba(99, 102, 241, 0.15) 25%,
      rgba(99, 102, 241, 0.15) 50%,
      rgba(99, 102, 241, 0.45) 50%,
      rgba(99, 102, 241, 0.45) 75%,
      rgba(99, 102, 241, 0.15) 75%,
      rgba(99, 102, 241, 0.15)
    );
    background-size: 14px 14px;
    animation: bar-stripes 1s linear infinite;
  }
  @keyframes bar-stripes {
    from { background-position: 0 0; }
    to   { background-position: 14px 0; }
  }
</style>
