<script lang="ts">
  import { AlertTriangle, ArrowRight, ArrowUp, Check, ChevronDown, ChevronRight, Copy, Eye, EyeOff, Pencil, Power, UserPlus, X } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { onDestroy } from 'svelte'

  import { wpBatches as batchesApi, wpCredentials as credApi, wpSites as sitesApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import DropdownMenu from '$lib/components/ui/DropdownMenu.svelte'
  import RoleLegend from '$lib/components/ui/RoleLegend.svelte'
  import type {
    WpCredential,
    WpImportResult,
    WpPoolSummary,
    WpSiteListItem,
    WpValidationState,
  } from '$lib/api/types'
  import { copyText } from '$lib/clipboard'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let items = $state<WpSiteListItem[]>([])
  let total = $state(0)
  // Пагинация (cursor-based): подгружаем следующие страницы кнопкой «Load more».
  const PER_PAGE = 200
  let nextCursor = $state<string | null>(null)
  let hasMore = $state(false)
  let loadingMore = $state(false)
  let summary = $state<WpPoolSummary>({
    sites_total: 0,
    sites_active: 0,
    credentials_total: 0,
    credentials_valid: 0,
    credentials_invalid: 0,
    credentials_pending: 0,
    credentials_transient: 0,
  })
  let loading = $state(true)

  let search = $state('')
  type SiteFilter =
    | 'all' | 'active' | 'auto-disabled' | 'off'
    | 'usable' | 'unusable' | 'cred_valid' | 'cred_invalid' | 'cred_transient'
    | 'rpc_postable' | 'admin_capable' | 'admin_postable'
  let filterStatus = $state<SiteFilter>('all')
  // Применить фильтр из клика по карточке + сразу перезагрузить.
  async function pickFilter(s: SiteFilter) {
    filterStatus = s
    await refresh()
  }
  let sortBy = $state<'alpha' | 'recent' | 'valid_desc' | 'transient_desc' | 'most_used'>('recent')

  // ─── Сортировка по колонкам (клиентская, поверх загруженной страницы) ──
  let colSortKey = $state<string | null>(null)
  let colSortDir = $state<'asc' | 'desc'>('asc')
  function toggleColSort(key: string) {
    if (colSortKey === key) colSortDir = colSortDir === 'asc' ? 'desc' : 'asc'
    else { colSortKey = key; colSortDir = 'asc' }
  }
  function siteSortVal(s: WpSiteListItem, key: string): string | number {
    switch (key) {
      case 'domain': return (s.domain ?? '').toLowerCase()
      case 'creds': return s.credentials_total
      case 'lang': return (s.language ?? '').toLowerCase()
      case 'used': return s.total_uses
      case 'check': return s.last_credential_check_at ? new Date(s.last_credential_check_at).getTime() : 0
      case 'status': return siteStatus(s).label
      default: return 0
    }
  }
  let sortedSites = $derived.by(() => {
    if (!colSortKey) return items
    const k = colSortKey, dir = colSortDir === 'asc' ? 1 : -1
    return [...items].sort((a, b) => {
      const va = siteSortVal(a, k), vb = siteSortVal(b, k)
      return va < vb ? -dir : va > vb ? dir : 0
    })
  })

  let isSuper = $derived($currentUser?.is_super_admin ?? false)

  // Expand site → load credentials lazily
  let expandedSiteId = $state<number | null>(null)
  let expandedCreds = $state<WpCredential[]>([])
  let expandedLoading = $state(false)

  // Create site
  let createSiteOpen = $state(false)
  let newDomain = $state('')
  let newHintPath = $state('')
  let newHintPort = $state<number | null>(null)
  let newNote = $state('')

  // Add credential under expanded site
  let addCredOpen = $state(false)
  let addCredForSite = $state<WpSiteListItem | null>(null)
  let credLogin = $state('')
  let credPassword = $state('')
  let credTag = $state('')

  // Edit site (hints/note)
  let editSite = $state<WpSiteListItem | null>(null)
  let edDomain = $state('')
  let edHintPath = $state('')
  let edHintPort = $state<number | null>(null)
  let edNote = $state('')

  // Validation state (Redis-backed singleton)
  let validation = $state<WpValidationState | null>(null)
  let validationPollTimer: ReturnType<typeof setInterval> | null = null

  async function refreshValidationStatus() {
    try {
      validation = await sitesApi.validationStatus()
    } catch {
      // тихо игнорим — UI остаётся как было
    }
  }

  function startValidationPolling() {
    if (validationPollTimer) return
    validationPollTimer = setInterval(async () => {
      // try/catch: необработанная ошибка в таймере во время Svelte-flush ломает
      // реактивность всей страницы (фильтры/таблица «замерзают»).
      try {
        await refreshValidationStatus()
        if (validation && !validation.running) {
          // финал — обновим саммари и список сайтов, остановим polling
          await loadSummary()
          await refresh()
          stopValidationPolling()
        }
      } catch (e) {
        console.warn('validation poll error', e)
      }
    }, 2000)
  }

  function stopValidationPolling() {
    if (validationPollTimer) {
      clearInterval(validationPollTimer)
      validationPollTimer = null
    }
  }

  async function triggerValidate(scope: 'all' | 'invalid' | 'transient' | 'stale') {
    if (validation?.running) {
      showToast('error', 'Validation already running')
      return
    }
    if (!confirm(`Запустить валидацию (scope: ${scope})? Это сделает по одному XML-RPC запросу на каждый credential.`)) return
    try {
      const res = await sitesApi.triggerValidate(scope)
      if (res.ok) {
        showToast('success', `Validation started (${scope})`)
      } else {
        showToast('error', res.message || 'Failed to start')
      }
      await refreshValidationStatus()
      startValidationPolling()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // ─── Bulk delete by filter ──────────────────────────────────────────
  async function bulkDeleteWithFilter(filter: { status?: string; search?: string }, label: string) {
    try {
      const { count } = await sitesApi.bulkFilterCount(filter)
      if (count === 0) {
        showToast('info', `Под фильтр «${label}» ничего не попадает`)
        return
      }
      if (!confirm(
        `Удалить ${count} credentials (${label})?\n\n` +
        `Это soft-delete — записи скрываются из пула, но остаются в БД для истории.\n` +
        `Действие audit-logged.`
      )) return
      const res = await sitesApi.bulkDeleteByFilter(filter)
      showToast('success', `Удалено ${res.deleted} credentials`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
  function bulkDeleteByStatus(status: 'invalid' | 'transient') {
    bulkDeleteWithFilter({ status }, `все ${status}`)
  }
  function bulkDeleteCurrentFilter() {
    // Текущая видимая выборка: status-mapping + search.
    // filterStatus у sites — про site (active/auto-disabled/off), не про cred.
    // Для bulk по cred используем search; статусный фильтр пока не маппим
    // 1:1 (site-статус ≠ cred-статус). Передаём только search.
    const f: { search?: string } = {}
    if (search.trim()) f.search = search.trim()
    if (!f.search) {
      showToast('error', 'Введи поисковый запрос — пустой фильтр снёс бы весь пул')
      return
    }
    bulkDeleteWithFilter(f, `поиск «${f.search}»`)
  }

  // ─── Provision (создание наших author-аккаунтов) ─────────────────────
  let provisionBusy = $state(false)

  async function bulkProvision() {
    if (provisionBusy) return
    let n = 0
    try {
      const c = await sitesApi.provisionCount()
      n = c.provisionable
    } catch { /* preview best-effort */ }
    if (n === 0) {
      showToast('info', 'Нет подходящих сайтов: нужен валидный admin-доступ с create_users и отсутствие нашего аккаунта')
      return
    }
    if (!confirm(
      `Создать наши author-аккаунты на ${n} сайтах?\n\n` +
      `Только там, где есть рабочий admin-доступ с правом create_users и нашего аккаунта ещё нет.\n` +
      `Запустится в фоне. Действие audit-logged.`
    )) return
    provisionBusy = true
    try {
      const r = await sitesApi.bulkProvision('author')
      showToast('success', `Provision запущен для ${r.provisionable} сайтов (фоном)`)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { provisionBusy = false }
  }

  async function provisionSite(site: WpSiteListItem) {
    if (provisionBusy) return
    if (!confirm(`Создать наш author-аккаунт на ${site.domain}?\n\nНужен рабочий admin-доступ с create_users.`)) return
    provisionBusy = true
    try {
      const r = await sitesApi.provisionSite(site.id, 'author')
      if (r.status === 'created') {
        showToast('success', `Создан аккаунт ${r.login} (${r.via}) на ${site.domain}`)
      } else if (r.status === 'skip_exists') {
        showToast('info', `На ${site.domain} наш аккаунт уже есть`)
      } else if (r.status === 'no_admin') {
        showToast('error', `На ${site.domain} нет валидного admin-доступа с create_users`)
      } else {
        showToast('error', `Не удалось (${r.status})${r.error ? ': ' + r.error : ''}`)
      }
      if (expandedSiteId === site.id) {
        expandedCreds = await credApi.listForSite(site.id, { include_password: isSuper })
      }
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { provisionBusy = false }
  }

  onDestroy(() => stopValidationPolling())

  // ─── UI helpers ──────────────────────────────────────────────────────

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

  function relTime(iso: string | null | undefined): string {
    if (!iso) return '—'
    const t = new Date(iso).getTime()
    const diff = Date.now() - t
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
    if (mo < 12) return `${mo}mo ago`
    return `${Math.floor(mo / 12)}y ago`
  }
  function fullTs(iso: string | null | undefined): string {
    return iso ? new Date(iso).toLocaleString() : ''
  }
  type SiteStatus =
    | { label: string; cls: string; title: string }
  /**
   * Operational verdict сайта — 4 категории:
   *   USABLE    — есть рабочий cred + домен жив → постить можно
   *   UNUSABLE  — домен мёртв ИЛИ все cred invalid → постить нельзя
   *   UNVERIFIED — провалидировали, но ни valid ни invalid (transient) →
   *                нужна повторная проверка чтобы решить
   *   PENDING   — ещё ни разу не валидировали (или нет cred)
   *
   * Согласовано с summary cards SITES USABLE / SITES UNUSABLE.
   */
  function siteStatus(s: WpSiteListItem): SiteStatus {
    const total = s.credentials_total ?? 0
    const v = s.credentials_valid ?? 0
    const i = s.credentials_invalid ?? 0
    const t = s.credentials_transient ?? 0
    const p = s.credentials_pending ?? 0

    // UNUSABLE: домен мёртв (auto-off) или все cred точно invalid
    if (!s.is_active || (total > 0 && i === total)) {
      const reason = !s.is_active
        ? `Домен auto-disabled ${s.auto_disabled_at ? fullTs(s.auto_disabled_at) : ''} (${s.last_site_failure_kind ?? 'fail'})`
        : 'Все cred невалидны — авторизация не работает'
      return { label: 'unusable', cls: 'bg-red-100 text-red-700', title: reason }
    }
    // USABLE: домен жив + хотя бы один cred точно valid
    if (v > 0) {
      const warn = (s.consecutive_site_failures ?? 0) > 0
      return {
        label: 'usable',
        cls: warn ? 'bg-emerald-100 text-emerald-700 ring-1 ring-amber-300' : 'bg-emerald-100 text-emerald-700',
        title: warn
          ? `Можно постить: ${v}/${total} cred рабочие. ⚠ ${s.consecutive_site_failures}/10 site-fail-ов накопилось.`
          : `Можно постить: ${v}/${total} cred рабочие`,
      }
    }
    // UNVERIFIED: были попытки, но ни valid ни decisive invalid
    if (t > 0) {
      return {
        label: 'unverified',
        cls: 'bg-amber-100 text-amber-700',
        title: `${t} cred с inconclusive результатом — прогони validate чтобы решить`,
      }
    }
    // PENDING: ещё не валидировали (или нет cred вообще)
    if (total === 0) {
      return { label: 'pending', cls: 'bg-slate-100 text-slate-500', title: 'Нет credentials' }
    }
    return { label: 'pending', cls: 'bg-slate-100 text-slate-500', title: `${p} cred ждут валидации` }
  }

  // Export credentials с расшифрованными паролями (super_admin only).
  // Как в экспорте батчей: 4 формата, экспортируем РОВНО то, что видно в таблице —
  // текущий фильтр (filterStatus) + поиск (search) уходят в query, бэкенд отдаёт
  // все credentials этих сайтов (валидность видна в колонке is_valid).
  function exportCreds(format: 'csv' | 'xlsx' | 'txt' | 'json') {
    const filtered = filterStatus !== 'all' || !!search
    const scope = filtered
      ? `Область: только показанное — фильтр «${filterStatus}»` +
        (search ? ` + поиск «${search}»` : '')
      : `Область: весь пул (фильтр не выбран)`
    const msg =
      `⚠ Скачать credentials с РАСшифрованными паролями?\n\n` +
      `Файл содержит секретные данные — храни безопасно и не передавай ` +
      `по незащищённым каналам.\n\n` +
      `Формат: ${format.toUpperCase()}\n` +
      scope
    if (!confirm(msg)) return
    const params = new URLSearchParams({ format })
    if (filterStatus !== 'all') params.set('status', filterStatus)
    if (search) params.set('search', search)
    // Триггерим скачивание — браузер увидит Content-Disposition: attachment
    // и не уйдёт со страницы.
    window.location.href = `/admin/api/wp-sites/export?${params.toString()}`
  }

  // Import CSV
  let importOpen = $state(false)
  let importFile = $state<File | null>(null)
  let importTag = $state('')
  let importBusy = $state(false)
  let importResult = $state<WpImportResult | null>(null)

  async function refresh() {
    loading = true
    try {
      const [list, s] = await Promise.all([
        sitesApi.list({
          limit: PER_PAGE,
          search: search || undefined,
          status: filterStatus,
          sort: sortBy,
        }),
        sitesApi.summary().catch(() => summary),
      ])
      items = list.items
      total = list.total
      nextCursor = list.next_cursor
      hasMore = list.has_more
      summary = s
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  // Подгрузить следующую страницу (append). Фильтр/поиск/сорт берём текущие —
  // они совпадают с тем, что отдал refresh (cursor валиден для той же выборки).
  async function loadMore() {
    if (!hasMore || !nextCursor || loadingMore) return
    loadingMore = true
    try {
      const list = await sitesApi.list({
        cursor: nextCursor,
        limit: PER_PAGE,
        search: search || undefined,
        status: filterStatus,
        sort: sortBy,
      })
      items = [...items, ...list.items]
      total = list.total
      nextCursor = list.next_cursor
      hasMore = list.has_more
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loadingMore = false
    }
  }
  // ─── Live counter heartbeat ──────────────────────────────────────────
  // Карточки сверху (CRED VALID/INVALID/...) — это снимок состояния, который
  // может «уплыть» если в этот момент кто-то ещё прогоняет batch validation,
  // или фоновый таск пересчитывает credentials. Поэтому держим лёгкий poll:
  //   — каждые 4 сек, когда что-то очевидно «работает» (есть active batches
  //     или сейчас идёт wp-sites validation), для live-feel
  //   — каждые 20 сек как fallback heartbeat (вдруг трогают БД snaружи)
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null
  let activeBatchCount = $state(0)
  async function refreshHeartbeat() {
    // Сначала проверим, есть ли что-то «активное» — батчи validating или
    // в реестре wp-sites singleton-валидация. Цена: 1 list-запрос.
    try {
      const r = await batchesApi.list()
      activeBatchCount = r.items.filter((b) => b.status === 'validating').length
    } catch {
      // тихо игнорим
    }
    // Сам summary (карточки). Во время активности — live (свежие цифры,
    // минуя MV); в покое — из materialized view (дёшево).
    const fresh = activeBatchCount > 0 || validation?.running
    try {
      summary = await sitesApi.summary({ live: !!fresh })
    } catch {}
  }

  // Загрузка только summary-карточек. После завершения валидации просим live —
  // получаем точный финальный snapshot не дожидаясь refresh MV.
  async function loadSummary(live = true) {
    try {
      summary = await sitesApi.summary({ live })
    } catch {}
  }

  onMount(async () => {
    await refresh()
    await refreshValidationStatus()
    // если валидация уже идёт (запустил другой админ) — подключаем polling
    if (validation?.running) startValidationPolling()

    let tick = 0
    heartbeatTimer = setInterval(async () => {
      tick += 1
      const fast = activeBatchCount > 0 || validation?.running
      if (fast || tick % 5 === 0) {
        await refreshHeartbeat()
      }
    }, 4000)
  })
  onDestroy(() => {
    if (heartbeatTimer) clearInterval(heartbeatTimer)
  })

  async function applyFilters(e: SubmitEvent) {
    e.preventDefault()
    await refresh()
  }

  // Какие cred-id сейчас «открыты» — пароли видны. Сбрасывается при collapse.
  let revealedCredIds = $state<Set<number>>(new Set())

  async function toggleExpand(site: WpSiteListItem) {
    if (expandedSiteId === site.id) {
      expandedSiteId = null
      expandedCreds = []
      revealedCredIds = new Set()
      return
    }
    expandedSiteId = site.id
    expandedLoading = true
    expandedCreds = []
    revealedCredIds = new Set()
    try {
      // Super_admin сразу подтягивает расшифрованные пароли (показ всё равно
      // под нажатием «глаза» — в DOM попадают, но visually masked).
      expandedCreds = await credApi.listForSite(site.id, { include_password: isSuper })
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      expandedLoading = false
    }
  }

  function toggleReveal(credId: number) {
    const next = new Set(revealedCredIds)
    if (next.has(credId)) next.delete(credId)
    else next.add(credId)
    revealedCredIds = next
  }

  async function copyToClipboard(text: string, label: string) {
    if (await copyText(text)) showToast('success', `${label} скопирован в буфер`)
    else showToast('error', 'Не получилось скопировать')
  }

  async function handleCreateSite(e: SubmitEvent) {
    e.preventDefault()
    try {
      await sitesApi.create({
        domain: newDomain,
        hint_path: newHintPath || undefined,
        hint_port: newHintPort ?? undefined,
        note: newNote || undefined,
      })
      showToast('success', `Site ${newDomain} created`)
      createSiteOpen = false
      newDomain = ''
      newHintPath = ''
      newHintPort = null
      newNote = ''
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function openAddCred(site: WpSiteListItem) {
    addCredForSite = site
    credLogin = ''
    credPassword = ''
    credTag = ''
    addCredOpen = true
  }

  async function handleAddCred(e: SubmitEvent) {
    e.preventDefault()
    if (!addCredForSite) return
    try {
      const tagsArr = credTag.split(',').map((t) => t.trim()).filter(Boolean)
      await credApi.create(addCredForSite.id, {
        login: credLogin,
        password: credPassword,
        tags: tagsArr.length > 0 ? tagsArr : undefined,
      })
      showToast('success', `Credential added`)
      addCredOpen = false
      // обновить credentials в раскрытом блоке + общую сводку
      if (expandedSiteId === addCredForSite.id) {
        expandedCreds = await credApi.listForSite(addCredForSite.id, { include_password: isSuper })
      }
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function deleteCred(c: WpCredential) {
    if (!confirm(`Delete credential ${c.login}?`)) return
    try {
      await credApi.remove(c.id)
      showToast('success', 'Deleted')
      if (expandedSiteId !== null) {
        expandedCreds = await credApi.listForSite(expandedSiteId, { include_password: isSuper })
      }
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function toggleCredValid(c: WpCredential) {
    try {
      await credApi.update(c.id, { is_valid: !c.is_valid })
      if (expandedSiteId !== null) {
        expandedCreds = await credApi.listForSite(expandedSiteId, { include_password: isSuper })
      }
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function openEditSite(s: WpSiteListItem) {
    editSite = s
    edDomain = s.domain
    edHintPath = s.hint_path ?? ''
    edHintPort = s.hint_port
    edNote = s.note ?? ''
  }

  async function saveEditSite() {
    if (!editSite) return
    try {
      await sitesApi.update(editSite.id, {
        domain: edDomain !== editSite.domain ? edDomain : undefined,
        hint_path: edHintPath !== (editSite.hint_path ?? '') ? edHintPath || null : undefined,
        hint_port: edHintPort !== editSite.hint_port ? edHintPort : undefined,
        note: edNote !== (editSite.note ?? '') ? edNote || null : undefined,
      })
      showToast('success', 'Updated')
      editSite = null
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function toggleSiteActive(s: WpSiteListItem) {
    try {
      await sitesApi.update(s.id, { is_active: !s.is_active })
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function deleteSite(s: WpSiteListItem) {
    if (!confirm(`Delete site ${s.domain} and ALL ${s.credentials_total} credentials?`)) return
    try {
      await sitesApi.remove(s.id)
      showToast('success', 'Site deleted')
      if (expandedSiteId === s.id) {
        expandedSiteId = null
        expandedCreds = []
      }
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function handleImport(e: SubmitEvent) {
    e.preventDefault()
    if (!importFile) return
    if (!confirm('Импортировать cred сразу как manual_valid (минуя валидацию)? Используй только если данные точно рабочие.')) return
    importBusy = true
    try {
      importResult = await sitesApi.importCsv(importFile, importTag || undefined)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      importBusy = false
    }
  }
  function closeImport() {
    importOpen = false
    importFile = null
    importTag = ''
    importResult = null
  }
</script>

<div class="space-y-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">WP Sites</h1>
      <p class="mt-1 text-sm text-slate-500">
        Сайты и доступы к ним. Один сайт может иметь много credentials (разные admin-логины).
      </p>
    </div>
    {#if isSuper}
      <div class="flex items-center gap-2">
        <DropdownMenu
          label={validation?.running ? '⟳ Validating…' : '✓ Validate'}
          disabled={validation?.running ?? false}
          title="Full-валидация: XML-RPC (Tier 1) + admin-login (Tier 2). Растит admin-канал. Медленнее, чем раньше."
          items={[
            {
              label: 'All valid',
              description: 'Full-перепроверка всех is_valid=true (RPC + admin). Долго!',
              onClick: () => triggerValidate('all'),
            },
            {
              label: 'Only invalid',
              description: 'Вдруг ожили — вернуть в строй (full: RPC + admin)',
              onClick: () => triggerValidate('invalid'),
            },
            {
              label: 'Only transient',
              description: 'Inconclusive — full-перепроверка (RPC + admin)',
              onClick: () => triggerValidate('transient'),
            },
            // 'Stale (>4h)' временно убран из UI (не пользоваться) — бэкенд scope
            // остаётся рабочим.
          ]}
        />
        {#if isSuper}
          <DropdownMenu
            label="🗑 Bulk delete"
            title="Массовое удаление credentials по статусу (необратимо)"
            items={[
              {
                label: 'Delete all invalid',
                description: 'Снести cred с нерабочей авторизацией',
                onClick: () => bulkDeleteByStatus('invalid'),
              },
              {
                label: 'Delete all transient',
                description: 'Снести неподтверждённые (осторожно — могут ожить)',
                onClick: () => bulkDeleteByStatus('transient'),
              },
              {
                label: 'Delete current filter',
                description: 'Всё что сейчас отфильтровано в таблице',
                onClick: () => bulkDeleteCurrentFilter(),
              },
            ]}
          />
        {/if}
        <button type="button" onclick={bulkProvision} disabled={provisionBusy}
                title="Создать наши author-аккаунты на всех сайтах, где есть admin-доступ с create_users и нашего аккаунта ещё нет"
                class="rounded-md border border-blue-300 bg-blue-50 px-3 py-1.5 text-sm font-medium text-blue-800 hover:bg-blue-100 disabled:opacity-50">
          {provisionBusy ? '⟳ Provisioning…' : '＋ Provision наши аккаунты'}
        </button>
        <DropdownMenu
          label="⤓ Export"
          title="Скачать credentials выбранного фильтра с расшифрованными паролями (super_admin only)"
          items={[
            {
              label: 'CSV',
              description: 'domain,url,login,password,… — по фильтру таблицы',
              onClick: () => exportCreds('csv'),
            },
            {
              label: 'Excel (XLSX)',
              description: 'Таблица для просмотра/правки',
              onClick: () => exportCreds('xlsx'),
            },
            {
              label: 'TXT (domain→url→login→pw)',
              description: 'Zebroid-формат, для ре-импорта',
              onClick: () => exportCreds('txt'),
            },
            {
              label: 'JSON',
              description: 'Массив объектов, для скриптов',
              onClick: () => exportCreds('json'),
            },
          ]}
        />
        <button onclick={() => (importOpen = true)}
                class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          <ArrowUp size={14} class="inline-block align-text-bottom" /> Import CSV
        </button>
        <button onclick={() => (createSiteOpen = true)}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
          + New site
        </button>
      </div>
    {/if}
  </div>

  <!-- Summary: 7 cards. «Cred valid» = подтверждено валидатором; pending —
       никогда не проверяли (default-valid после import); transient — был
       ответ, но не conclusive (network/parked/timeout). -->
  <!-- Sites blocks: operational verdict (постить или нет).
       SITES USABLE = домен жив + ≥1 valid cred.
       SITES UNUSABLE = домен off ИЛИ все cred невалидны. -->
  <!-- Карточки = кликабельные фильтры. Usable/Unusable объединяют sites+creds.
       Channels — разбивка рабочих cred по каналам (XML-RPC / admin). -->
  <div class="grid gap-3 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-5">
    <button type="button" onclick={() => pickFilter('all')}
            class="rounded-lg border border-slate-200 bg-white p-4 text-left transition hover:bg-slate-50 {filterStatus === 'all' ? 'ring-2 ring-slate-400' : ''}">
      <div class="text-xs uppercase tracking-wider text-slate-500">Sites total</div>
      <div class="mt-1 text-2xl font-semibold text-slate-900">{summary.sites_total}</div>
    </button>

    <button type="button" onclick={() => pickFilter('usable')}
            title="Usable = логин валиден (домен жив + ≥1 valid cred). НО постить можно только там, где подтверждён канал (XML-RPC или wp-admin) — это «postable». Валидный вход ≠ постинг."
            class="rounded-lg border border-emerald-200 bg-emerald-50/50 p-4 text-left transition hover:bg-emerald-50 {filterStatus === 'usable' || filterStatus === 'cred_valid' ? 'ring-2 ring-emerald-400' : ''}">
      <div class="text-xs uppercase tracking-wider text-emerald-700">Usable <span class="font-normal text-slate-400">(логин)</span></div>
      <div class="mt-1 flex items-baseline gap-2">
        <span class="text-2xl font-semibold text-emerald-700">{summary.sites_usable ?? 0}</span>
        <span class="text-xs text-slate-500">sites · <span class="font-medium text-emerald-600">{summary.credentials_valid}</span> creds</span>
      </div>
      <div class="mt-0.5 text-[11px] text-slate-500">
        <span class="font-semibold text-emerald-700">{summary.sites_postable ?? 0}</span> postable
        <span class="text-slate-400">· подтверждён канал постинга</span>
      </div>
    </button>

    <button type="button" onclick={() => pickFilter('unusable')}
            title="Не пригодны: домен off ИЛИ все cred невалидны"
            class="rounded-lg border border-red-200 bg-red-50/50 p-4 text-left transition hover:bg-red-50 {filterStatus === 'unusable' || filterStatus === 'cred_invalid' ? 'ring-2 ring-red-400' : ''}">
      <div class="text-xs uppercase tracking-wider text-red-700">Unusable</div>
      <div class="mt-1 flex items-baseline gap-2">
        <span class="text-2xl font-semibold text-red-700">{summary.sites_unusable ?? 0}</span>
        <span class="text-xs text-slate-500">sites · <span class="font-medium text-red-600">{summary.credentials_invalid}</span> bad creds</span>
      </div>
    </button>

    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="text-xs uppercase tracking-wider text-slate-500">Channels (pool)</div>
      <div class="mt-1 flex items-baseline gap-2">
        <!-- Клик по каналу = фильтр по реальному пулу. rpc → постинг, admin → ссылки. -->
        <button type="button" onclick={() => pickFilter('rpc_postable')}
                title="Сайты с cred, постящим через XML-RPC (пул постинга). Клик — отфильтровать."
                class="rounded px-1.5 py-0.5 text-2xl font-semibold text-slate-900 transition hover:bg-slate-100 {filterStatus === 'rpc_postable' ? 'ring-2 ring-slate-400' : ''}">
          {summary.credentials_valid_rpc ?? 0}<span class="ml-1 text-xs font-normal text-slate-400">rpc</span>
        </button>
        <span class="text-slate-300">·</span>
        <button type="button" onclick={() => pickFilter('admin_capable')}
                title="Сайты с cred, логинящимся в wp-admin — ПУЛ ССЫЛОК (can_admin_login). Клик — отфильтровать."
                class="rounded px-1.5 py-0.5 text-2xl font-semibold text-slate-900 transition hover:bg-slate-100 {filterStatus === 'admin_capable' ? 'ring-2 ring-purple-400' : ''}">
          {summary.credentials_valid_admin ?? 0}<span class="ml-1 text-xs font-normal text-slate-400">admin</span>
        </button>
      </div>
    </div>

    <button type="button" onclick={() => pickFilter('cred_transient')}
            title="Inconclusive cred (network/timeout/CF) → нужна повторная проверка"
            class="rounded-lg border border-amber-200 bg-amber-50/50 p-4 text-left transition hover:bg-amber-50 {filterStatus === 'cred_transient' ? 'ring-2 ring-amber-400' : ''}">
      <div class="text-xs uppercase tracking-wider text-amber-700">Transient</div>
      <div class="mt-1 text-2xl font-semibold text-amber-700">{summary.credentials_transient}</div>
    </button>
  </div>

  <!-- Validation progress banner -->
  {#if validation && (validation.running || validation.done > 0)}
    {@const pct = validation.total ? Math.round((validation.done * 100) / validation.total) : 0}
    <div class="rounded-lg border border-brand-200 bg-brand-50/40 px-4 py-3 text-sm">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          {#if validation.running}
            <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-brand-600"></span>
            <strong class="text-brand-800">Validation running</strong>
          {:else}
            <span class="inline-block h-2 w-2 rounded-full bg-emerald-600"></span>
            <strong class="text-emerald-800">Last validation done</strong>
          {/if}
          <span class="text-slate-500">scope: <code class="rounded bg-white px-1">{validation.scope}</code></span>
        </div>
        <span class="text-xs text-slate-500">
          {#if validation.started_at}started {new Date(validation.started_at).toLocaleString()}{/if}
          {#if validation.finished_at} · finished {new Date(validation.finished_at).toLocaleString()}{/if}
        </span>
      </div>
      <div class="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
        <div class="h-full bg-brand-500 transition-all" style="width: {pct}%"></div>
      </div>
      <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
        <span><strong>{validation.done}</strong> / {validation.total} ({pct}%)</span>
        <span class="text-emerald-700"><Check size={14} class="inline-block align-text-bottom" /> valid: {validation.valid}</span>
        <span class="text-red-700"><X size={14} class="inline-block align-text-bottom" /> invalid: {validation.invalid}</span>
        {#if validation.transient_errors > 0}
          <span class="text-amber-700"><AlertTriangle size={14} class="inline-block align-text-bottom" /> transient: {validation.transient_errors}</span>
        {/if}
      </div>
    </div>
  {/if}

  <!-- Filters -->
  <form onsubmit={applyFilters} class="flex flex-wrap items-end gap-3">
    <div class="min-w-[200px] flex-1">
      <label for="wp_search" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Search</label>
      <input id="wp_search" type="search" bind:value={search}
             placeholder="domain"
             class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
    </div>
    <div>
      <label for="wp_status" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Status</label>
      <select id="wp_status" bind:value={filterStatus}
              onchange={(e) => { filterStatus = e.currentTarget.value as SiteFilter; refresh() }}
              class="mt-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm">
        <option value="all">all</option>
        <optgroup label="operational">
          <option value="usable">usable (готовы к постингу)</option>
          <option value="unusable">unusable</option>
        </optgroup>
        <optgroup label="channels (реальный пул)">
          <option value="rpc_postable">RPC-postable (пул постинга)</option>
          <option value="admin_capable">Admin-capable (пул ссылок)</option>
          <option value="admin_postable">Admin-postable (постинг через admin)</option>
        </optgroup>
        <optgroup label="by cred status">
          <option value="cred_valid">has valid cred</option>
          <option value="cred_invalid">has invalid cred</option>
          <option value="cred_transient">has transient cred</option>
        </optgroup>
        <optgroup label="by domain">
          <option value="active">active (domain alive)</option>
          <option value="auto-disabled">auto-disabled</option>
          <option value="off">off (manual)</option>
        </optgroup>
      </select>
    </div>
    <div>
      <label for="wp_sort" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Sort</label>
      <select id="wp_sort" bind:value={sortBy}
              onchange={(e) => { sortBy = e.currentTarget.value as typeof sortBy; refresh() }}
              class="mt-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm">
        <option value="recent">recent check</option>
        <option value="alpha">alphabetical</option>
        <option value="valid_desc">most valid first</option>
        <option value="transient_desc">most transient first</option>
        <option value="most_used">most used first</option>
      </select>
    </div>
    <button class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
      Apply
    </button>
  </form>

  <!-- Table -->
  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      No sites. {#if isSuper}Import CSV или добавь вручную.{/if}
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      {#snippet sortTh(key: string, label: string, cls = '')}
        <th class="px-4 py-2 {cls}">
          <button type="button" onclick={() => toggleColSort(key)}
                  class="inline-flex items-center gap-0.5 uppercase tracking-wider transition hover:text-slate-700"
                  class:text-brand-700={colSortKey === key}>
            {label}
            <span class="text-[10px]">{colSortKey === key ? (colSortDir === 'asc' ? '↑' : '↓') : '↕'}</span>
          </button>
        </th>
      {/snippet}
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="w-8"></th>
            {@render sortTh('domain', 'Domain')}
            {@render sortTh('creds', 'Creds', 'text-center')}
            <th class="px-4 py-2 text-center" title="Каналы сайта: XML-RPC · admin form-login (✓ работает · ✕ нет · — не проверяли)">Channels</th>
            {@render sortTh('lang', 'Lang', 'text-center')}
            {@render sortTh('used', 'Used', 'text-center')}
            {@render sortTh('check', 'Last check', 'text-center')}
            {@render sortTh('status', 'Status', 'text-center')}
            {#if isSuper}<th class="px-4 py-2 text-right">Actions</th>{/if}
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each sortedSites as site (site.id)}
            {@const isExp = expandedSiteId === site.id}
            {@const total = site.credentials_total}
            {@const v = site.credentials_valid}
            {@const i = site.credentials_invalid}
            {@const t = site.credentials_transient}
            {@const p = site.credentials_pending}
            {@const prov = site.credentials_provisioned ?? 0}
            {@const st = siteStatus(site)}
            <tr class="align-top hover:bg-slate-50">
              <td class="px-2 pt-3 text-center">
                <button onclick={() => toggleExpand(site)}
                        class="text-slate-400 hover:text-slate-700"
                        title={isExp ? 'Collapse' : 'Expand'}>
                  {#if isExp}<ChevronDown size={14} />{:else}<ChevronRight size={14} />{/if}
                </button>
              </td>
              <td class="px-4 py-2 font-mono text-xs">
                <a href={`/wp-sites/${site.id}`} class="text-slate-800 hover:text-brand-600 hover:underline">
                  {site.domain}
                </a>
                {#if site.note}
                  <div class="mt-0.5 text-[10px] text-slate-400 line-clamp-1" title={site.note}>{site.note}</div>
                {/if}
              </td>
              <td class="px-4 py-2 text-center text-xs font-medium text-slate-700">
                {#if total > 0}
                  {#if prov}
                    <span title={`${total - prov} исходн. + ${prov} наш(их) (provision-author)`}>
                      <span class="text-slate-700">{total - prov}</span><span class="font-semibold text-blue-600"> +{prov}</span>
                    </span>
                    {#if i || t || p}
                      <div class="text-[10px] text-slate-400">
                        {#if i}<span class="text-red-500">{i} bad</span>{/if}{#if i && (t || p)} · {/if}{#if t}<span class="text-amber-600">{t}</span>{/if}{#if t && p} · {/if}{#if p}<span class="text-slate-500">{p}</span>{/if}
                      </div>
                    {/if}
                  {:else}
                    {total}
                    <div class="text-[10px] text-slate-400" title="Valid · Invalid · Transient · Pending">
                      {#if v}<span class="text-emerald-600">{v}</span>{/if}{#if v && (i || t || p)} · {/if}{#if i}<span class="text-red-500">{i}</span>{/if}{#if i && (t || p)} · {/if}{#if t}<span class="text-amber-600">{t}</span>{/if}{#if t && p} · {/if}{#if p}<span class="text-slate-500">{p}</span>{/if}
                    </div>
                  {/if}
                {:else}<span class="text-slate-300">—</span>{/if}
              </td>
              <td class="px-4 py-2 text-center">
                <!-- Channel matrix агрегат по cred сайта: RPC · Admin -->
                <div class="flex items-center justify-center gap-1.5 text-[11px]">
                  <span class="inline-flex items-center gap-0.5"
                        title={site.site_can_post_via_xmlrpc === true
                          ? 'XML-RPC: логин работает — можно постить'
                          : site.site_can_xmlrpc === true
                            ? 'XML-RPC: эндпоинт жив, но логин не прошёл'
                            : site.site_can_xmlrpc === false
                              ? 'XML-RPC: отключён / недоступен'
                              : 'XML-RPC: ещё не проверяли'}>
                    <span class="text-slate-400">RPC</span>
                    {#if site.site_can_post_via_xmlrpc === true}<span class="text-emerald-600">✓</span>
                    {:else if site.site_can_xmlrpc === true}<span class="text-amber-500">⚠</span>
                    {:else if site.site_can_xmlrpc === false}<span class="text-red-500">✕</span>
                    {:else}<span class="text-slate-300">—</span>{/if}
                  </span>
                  <span class="text-slate-200">·</span>
                  <span class="inline-flex items-center gap-0.5"
                        title="Admin login (Tier 2): {site.site_can_admin === true ? 'работает' : site.site_can_admin === false ? 'не сработал' : 'не проверяли'}">
                    <span class="text-slate-400">Adm</span>
                    {#if site.site_can_admin === true}<span class="text-emerald-600">✓</span>
                    {:else if site.site_can_admin === false}<span class="text-red-500">✕</span>
                    {:else}<span class="text-slate-300">—</span>{/if}
                  </span>
                </div>
              </td>
              <td class="px-4 py-2 text-center">
                {#if site.language}
                  <span class="rounded-md bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-700"
                        title={site.language_detected_at ? 'Detected ' + relTime(site.language_detected_at) : ''}>{site.language}</span>
                {:else if site.language_detected_at}
                  <span class="rounded-md bg-slate-50 px-1.5 py-0.5 text-[10px] italic text-slate-400"
                        title="Lang detect был запущен, но язык не определён (пустая SPA-страница, mojibake или мало текста)">none</span>
                {:else}
                  <span class="text-[10px] text-slate-300" title="Не пытались определить">—</span>
                {/if}
              </td>
              <td class="px-4 py-2 text-center text-xs"
                  title={site.last_used_at ? 'Last used ' + fullTs(site.last_used_at) : 'Never used'}>
                {#if site.total_uses > 0}
                  <span class="font-medium text-slate-800">{site.total_uses}</span>
                  {#if site.last_used_at}
                    <div class="text-[10px] text-slate-400">{relTime(site.last_used_at)}</div>
                  {/if}
                {:else}
                  <span class="text-slate-300">—</span>
                {/if}
              </td>
              <td class="px-4 py-2 text-center text-xs text-slate-500"
                  title={fullTs(site.last_credential_check_at)}>
                {relTime(site.last_credential_check_at)}
              </td>
              <td class="px-4 py-2 text-center">
                <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {st.cls}"
                      title={st.title}>{st.label}</span>
              </td>
              {#if isSuper}
                <td class="px-4 py-2 text-right whitespace-nowrap">
                  <button onclick={() => openAddCred(site)} title="Add credential"
                          class="inline-flex h-7 px-2 items-center rounded-md bg-brand-50 text-xs text-brand-700 hover:bg-brand-100">
                    + cred
                  </button>
                  <button onclick={() => provisionSite(site)} disabled={provisionBusy}
                          title="Создать наш author-аккаунт на этом сайте (нужен admin-доступ с create_users)"
                          class="ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md bg-blue-50 text-blue-700 hover:bg-blue-100 disabled:opacity-50">
                    <UserPlus size={14} class="inline-block align-text-bottom" />
                  </button>
                  <button onclick={() => openEditSite(site)} title="Edit hints/note"
                          class="ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md bg-emerald-50 text-emerald-700 hover:bg-emerald-100">
                    <Pencil size={14} class="inline-block align-text-bottom" />
                  </button>
                  <button onclick={() => toggleSiteActive(site)} title={site.is_active ? 'Deactivate' : 'Activate'}
                          class="ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md bg-brand-50 text-brand-700 hover:bg-brand-100">
                    <Power size={14} class="inline-block align-text-bottom" />
                  </button>
                  <button onclick={() => deleteSite(site)} title="Delete site + all credentials"
                          class="ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md bg-red-50 text-red-700 hover:bg-red-100">
                    <X size={14} class="inline-block align-text-bottom" />
                  </button>
                </td>
              {/if}
            </tr>

            {#if isExp}
              <tr class="bg-slate-50/50">
                <td></td>
                <td colspan={isSuper ? 8 : 7} class="px-4 py-3">
                  <!-- Site debug strip -->
                  <div class="mb-3 grid grid-cols-2 gap-x-6 gap-y-1 text-[11px] text-slate-600 md:grid-cols-3 lg:grid-cols-4">
                    <div>
                      <span class="text-slate-400">Hint path:</span>
                      <code class="ml-1 rounded bg-white px-1">{site.hint_path ?? '—'}</code>
                    </div>
                    <div>
                      <span class="text-slate-400">Hint port:</span>
                      <code class="ml-1 rounded bg-white px-1">{site.hint_port ?? '—'}</code>
                    </div>
                    <div class="col-span-2 lg:col-span-2">
                      <span class="text-slate-400">XML-RPC cache:</span>
                      {#if site.last_working_url}
                        <code class="ml-1 rounded bg-emerald-50 px-1 text-emerald-700" title={site.last_working_url}>{site.last_working_url.length > 60 ? site.last_working_url.slice(0, 60) + '…' : site.last_working_url}</code>
                        <span class="ml-1 text-slate-400">({relTime(site.last_working_at)})</span>
                      {:else}
                        <span class="ml-1 text-slate-400">— (will discover on next call)</span>
                      {/if}
                    </div>
                    {#if site.language}
                      <div>
                        <span class="text-slate-400">Language:</span>
                        <code class="ml-1 rounded bg-white px-1">{site.language}</code>
                        {#if site.language_detected_at}
                          <span class="ml-1 text-slate-400">({relTime(site.language_detected_at)})</span>
                        {/if}
                      </div>
                    {/if}
                    {#if (site.consecutive_site_failures ?? 0) > 0 || site.auto_disabled_at}
                      <div class="col-span-2 lg:col-span-2">
                        <span class="text-slate-400">Site fails:</span>
                        <span class="ml-1 text-amber-700">{site.consecutive_site_failures ?? 0}</span>
                        {#if site.last_site_failure_kind}
                          <code class="ml-2 rounded bg-amber-50 px-1 text-amber-700">{site.last_site_failure_kind}</code>
                        {/if}
                        {#if site.last_site_failure_at}
                          <span class="ml-1 text-slate-400">({relTime(site.last_site_failure_at)})</span>
                        {/if}
                        {#if site.auto_disabled_at}
                          <span class="ml-2 rounded bg-red-50 px-1 text-red-700">auto-disabled {relTime(site.auto_disabled_at)}</span>
                        {/if}
                      </div>
                    {/if}
                    {#if site.note}
                      <div class="col-span-full">
                        <span class="text-slate-400">Note:</span>
                        <span class="ml-1 text-slate-700">{site.note}</span>
                      </div>
                    {/if}
                  </div>
                  {#if expandedLoading}
                    <div class="text-xs text-slate-500">Loading credentials…</div>
                  {:else if expandedCreds.length === 0}
                    <div class="text-xs text-slate-500">No credentials. {#if isSuper}Add one above.{/if}</div>
                  {:else}
                    <table class="min-w-full text-xs">
                      <thead>
                        <tr class="text-left text-slate-500">
                          <th class="px-2 py-1">Login</th>
                          {#if isSuper}<th class="px-2 py-1">Password</th>{/if}
                          <th class="px-2 py-1">Tag</th>
                          <th class="px-2 py-1 text-center" title="XML-RPC канал · admin form-login канал (✓ работает · ✕ нет · — не проверяли)">Channels</th>
                          <th class="px-2 py-1 text-center" title="WP-роль (XML-RPC wp.getProfile / админ-меню)">
                            <span class="inline-flex items-center gap-1">Role<RoleLegend align="left" /></span>
                          </th>
                          <th class="px-2 py-1 text-center">Used</th>
                          <th class="px-2 py-1">Outcome / Error</th>
                          <th class="px-2 py-1 text-center">Status</th>
                          {#if isSuper}<th class="px-2 py-1 text-right">Actions</th>{/if}
                        </tr>
                      </thead>
                      <tbody>
                        {#each expandedCreds as c (c.id)}
                          {@const isRevealed = revealedCredIds.has(c.id)}
                          <tr class:bg-blue-50={c.provisioned}>
                            <td class="px-2 py-1 font-mono">
                              {c.login}
                              {#if c.provisioned}
                                <span class="ml-1 rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700"
                                      title={`Создан нами (provision-author${c.provisioned_via ? ', через ' + c.provisioned_via : ''})`}>＋ наш</span>
                              {/if}
                              {#if isSuper}
                                <button onclick={() => copyToClipboard(c.login, 'Login')}
                                        title="Copy login"
                                        class="ml-1 text-slate-400 hover:text-slate-700"><Copy size={14} class="inline-block align-text-bottom" /></button>
                              {/if}
                            </td>
                            {#if isSuper}
                              <td class="px-2 py-1 font-mono">
                                {#if c.password}
                                  <span class="select-all">{isRevealed ? c.password : '••••••••'}</span>
                                  <button onclick={() => toggleReveal(c.id)}
                                          title={isRevealed ? 'Hide' : 'Show'}
                                          class="ml-1 text-slate-400 hover:text-slate-700">{#if isRevealed}<EyeOff size={14} />{:else}<Eye size={14} />{/if}</button>
                                  <button onclick={() => copyToClipboard(c.password!, 'Password')}
                                          title="Copy password"
                                          class="ml-1 text-slate-400 hover:text-slate-700"><Copy size={14} class="inline-block align-text-bottom" /></button>
                                {:else}
                                  <span class="text-slate-300" title="Не удалось расшифровать или пустой">—</span>
                                {/if}
                              </td>
                            {/if}
                            <td class="px-2 py-1">
                              {#if c.tags && c.tags.length > 0}
                                <div class="flex flex-wrap gap-1">
                                  {#each c.tags as t}
                                    <span class="rounded-md bg-slate-100 px-1.5 py-0.5 text-[10px]">{t}</span>
                                  {/each}
                                </div>
                              {:else}—{/if}
                            </td>
                            <td class="px-2 py-1 text-center">
                              <!-- Channel matrix: XML-RPC + admin form-login -->
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
                                  {#if c.can_post_via_xmlrpc === true}<span class="text-emerald-600">✓</span>
                                  {:else if c.can_xmlrpc === true}<span class="text-amber-500">⚠</span>
                                  {:else if c.can_xmlrpc === false}<span class="text-red-500">✕</span>
                                  {:else}<span class="text-slate-300">—</span>{/if}
                                </span>
                                <span class="text-slate-200">·</span>
                                <span class="inline-flex items-center gap-0.5"
                                      title="Admin form-login (Tier 2): {c.can_admin_login === true ? 'работает' : c.can_admin_login === false ? 'не сработал' : 'не проверяли'}">
                                  <span class="text-slate-400">Admin</span>
                                  {#if c.can_admin_login === true}<span class="text-emerald-600">✓</span>
                                  {:else if c.can_admin_login === false}<span class="text-red-500">✕</span>
                                  {:else}<span class="text-slate-300">—</span>{/if}
                                </span>
                              </div>
                            </td>
                            <td class="px-2 py-1 text-center">
                              {#if c.admin_role}
                                {@const rb = roleBadge(c.admin_role)}
                                <span class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {rb.cls}"
                                      title={c.can_create_users ? 'create_users ✓' : ''}>
                                  {rb.label}{#if c.can_create_users}＋{/if}
                                </span>
                              {:else}<span class="text-slate-300">—</span>{/if}
                            </td>
                            <td class="px-2 py-1 text-center">{c.amount_use}</td>
                            <td class="px-2 py-1 text-xs max-w-xs">
                              {#if c.last_error_message}
                                <!-- явная ошибка (auth fail / network / etc) -->
                                {#if c.last_validation_kind}
                                  <code class="rounded bg-slate-100 px-1 text-[10px] text-slate-700">{c.last_validation_kind}</code>
                                {/if}
                                <div class="truncate text-red-600" title={c.last_error_message}>{c.last_error_message}</div>
                                {#if c.error_counter && c.error_counter > 0}
                                  <div class="text-[10px] text-slate-400">×{c.error_counter} fails</div>
                                {/if}
                              {:else if c.last_validation_kind && c.last_validation_kind !== 'ok' && c.last_validation_kind !== 'manual_valid'}
                                <!-- valid·admin но XML-RPC дал диагностику (xmlrpc_disabled/broken_endpoint) -->
                                <code class="rounded bg-amber-50 px-1 text-[10px] text-amber-700"
                                      title="XML-RPC канал: {c.last_validation_kind}. Cred рабочий через admin — постинг пойдёт Tier 2.">{c.last_validation_kind}</code>
                              {:else if c.last_validation_kind === 'ok'}
                                <span class="text-[11px] text-emerald-600">OK</span>
                              {:else}
                                <span class="text-slate-300">—</span>
                              {/if}
                            </td>
                            <td class="px-2 py-1 text-center">
                              {#if !c.is_valid}
                                <span class="rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-red-700"
                                      title={c.last_validation_kind === 'auth_invalid' || c.last_validation_kind === 'permission_denied'
                                        ? 'Wrong username / password' : 'Marked invalid'}>
                                  {c.last_validation_kind === 'auth_invalid' || c.last_validation_kind === 'permission_denied' ? 'auth fail' : 'invalid'}
                                </span>
                              {:else if c.last_validation_kind === 'ok' || c.last_validation_kind === 'manual_valid'}
                                <span class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-emerald-700"
                                      title={c.last_validation_kind === 'manual_valid'
                                        ? 'Импортирован как доверенный — НЕ проверен системой через Tier 1/2. Запустите валидацию, чтобы подтвердить'
                                        : 'XML-RPC ответил успехом'}>
                                  {c.last_validation_kind === 'manual_valid' ? 'manual' : 'valid'}
                                </span>
                              {:else if c.can_admin_login === true}
                                <span class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-emerald-700"
                                      title="Подтверждён через admin login (Tier 2)">valid · admin</span>
                              {:else if c.last_validated_at}
                                <span class="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-amber-700"
                                      title={c.last_validation_kind || 'inconclusive — ни один канал не подтвердил'}>transient</span>
                              {:else}
                                <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-slate-500">pending</span>
                              {/if}
                            </td>
                            {#if isSuper}
                              <td class="px-2 py-1 text-right whitespace-nowrap">
                                <button onclick={() => toggleCredValid(c)}
                                        class="text-xs text-brand-600 hover:underline">toggle</button>
                                <button onclick={() => deleteCred(c)}
                                        class="ml-2 text-xs text-red-600 hover:underline">delete</button>
                              </td>
                            {/if}
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
    <div class="mt-3 flex flex-wrap items-center justify-center gap-3">
      <span class="text-xs text-slate-500">Showing {items.length} of {total}</span>
      {#if hasMore}
        <button type="button" onclick={loadMore} disabled={loadingMore}
                class="rounded-md border border-brand-300 bg-brand-50 px-3 py-1.5 text-xs font-medium text-brand-700 hover:bg-brand-100 disabled:opacity-50">
          {loadingMore ? 'Загрузка…' : `Показать ещё (+${PER_PAGE})`}
        </button>
      {/if}
    </div>
  {/if}
</div>

<!-- Create site modal -->
{#if createSiteOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createSiteOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">New site</h2>
      <p class="mt-1 text-xs text-slate-500">Сайт хранится bare. Credentials добавишь после создания.</p>
      <form onsubmit={handleCreateSite} class="mt-4 space-y-3">
        <div>
          <label for="ns_domain" class="block text-sm font-medium text-slate-700">Domain *</label>
          <input id="ns_domain" type="text" bind:value={newDomain} required
                 placeholder="example.com"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label for="ns_path" class="block text-sm font-medium text-slate-700">
              Hint path <span class="text-slate-400">(если WP в /blog)</span>
            </label>
            <input id="ns_path" type="text" bind:value={newHintPath} maxlength="200"
                   placeholder="/blog"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
          </div>
          <div>
            <label for="ns_port" class="block text-sm font-medium text-slate-700">
              Hint port <span class="text-slate-400">(если не 80/443)</span>
            </label>
            <input id="ns_port" type="number" bind:value={newHintPort} min="1" max="65535"
                   placeholder="8080"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
          </div>
        </div>
        <div>
          <label for="ns_note" class="block text-sm font-medium text-slate-700">Note</label>
          <input id="ns_note" type="text" bind:value={newNote}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createSiteOpen = false)}
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

<!-- Add credential modal -->
{#if addCredOpen && addCredForSite}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (addCredOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Add credential</h2>
      <p class="mt-1 text-sm text-slate-500">For site <code class="font-mono">{addCredForSite.domain}</code></p>
      <form onsubmit={handleAddCred} class="mt-4 space-y-3">
        <div>
          <label for="ac_login" class="block text-sm font-medium text-slate-700">Login *</label>
          <input id="ac_login" type="text" bind:value={credLogin} required
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="ac_pwd" class="block text-sm font-medium text-slate-700">Password *</label>
          <input id="ac_pwd" type="text" bind:value={credPassword} required
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
        </div>
        <div>
          <label for="ac_tag" class="block text-sm font-medium text-slate-700">Tags</label>
          <input id="ac_tag" type="text" bind:value={credTag} maxlength="500"
                 placeholder="english, tech, mass-jan-2025"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <p class="mt-1 text-[11px] text-slate-400">Несколько тегов через запятую.</p>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (addCredOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Cancel
          </button>
          <button type="submit" class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">
            Add
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Edit site modal -->
{#if editSite}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (editSite = null)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Edit site</h2>
      <div class="mt-4 space-y-3">
        <div>
          <label for="es_domain" class="block text-sm font-medium text-slate-700">Domain</label>
          <input id="es_domain" type="text" bind:value={edDomain}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label for="es_path" class="block text-sm font-medium text-slate-700">Hint path</label>
            <input id="es_path" type="text" bind:value={edHintPath} placeholder="/blog"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
          </div>
          <div>
            <label for="es_port" class="block text-sm font-medium text-slate-700">Hint port</label>
            <input id="es_port" type="number" bind:value={edHintPort} min="1" max="65535"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
          </div>
        </div>
        <div>
          <label for="es_note" class="block text-sm font-medium text-slate-700">Note</label>
          <input id="es_note" type="text" bind:value={edNote}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
      </div>
      <div class="mt-6 flex justify-end gap-2">
        <button type="button" onclick={() => (editSite = null)}
                class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
          Cancel
        </button>
        <button type="button" onclick={saveEditSite}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">
          Save
        </button>
      </div>
    </div>
  </div>
{/if}

<!-- Import CSV modal -->
{#if importOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={closeImport}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Import credentials from CSV</h2>
      <p class="mt-1 text-xs text-slate-500">
        Формат: <code class="rounded bg-slate-100 px-1">domain,login,password</code>.
        Сайты создаются автоматически, дубль credentials (site+login) пропускается.
      </p>
      {#if !importResult}
        <div class="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
          <div class="flex items-start gap-2">
            <AlertTriangle size={14} class="mt-0.5 shrink-0" />
            <div>
              <strong>Этот импорт минует валидацию.</strong>
              Cred сразу помечаются <code class="rounded bg-amber-100 px-1">manual_valid</code> —
              т.е. вы подтверждаете, что данные точно рабочие.
              Для безопасной проверки большой пачки данных
              используйте <strong>Batches</strong> — пройдут Tier 1/2/3.
            </div>
          </div>
        </div>
      {/if}

      {#if importResult}
        <div class="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
          <div><Check size={14} class="inline-block align-text-bottom" /> Imported credentials: <strong>{importResult.imported_credentials}</strong></div>
          <div>Sites created: {importResult.sites_created}</div>
          <div>Sites touched: {importResult.sites_touched}</div>
          <div>Skipped duplicate credentials: {importResult.skipped_duplicate_credentials}</div>
          <div>Skipped invalid rows: {importResult.skipped_invalid_rows}</div>
          <div class="mt-1 text-xs text-emerald-600">Total rows in file: {importResult.total_rows}</div>
        </div>
        <div class="mt-4 flex justify-end">
          <button onclick={closeImport}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Done</button>
        </div>
      {:else}
        <form onsubmit={handleImport} class="mt-4 space-y-3">
          <div>
            <label for="imp_file" class="block text-sm font-medium text-slate-700">CSV file *</label>
            <input id="imp_file" type="file" accept=".csv,text/csv" required
                   onchange={(e) => { importFile = (e.currentTarget as HTMLInputElement).files?.[0] ?? null }}
                   class="mt-1 w-full text-sm" />
          </div>
          <div>
            <label for="imp_tag" class="block text-sm font-medium text-slate-700">
              Tag <span class="text-slate-400">(к каждому credential)</span>
            </label>
            <input id="imp_tag" type="text" bind:value={importTag} maxlength="100"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
          <div class="flex justify-end gap-2 pt-2">
            <button type="button" onclick={closeImport}
                    class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
              Cancel
            </button>
            <button type="submit" disabled={importBusy || !importFile}
                    class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
              {importBusy ? 'Importing…' : 'Import'}
            </button>
          </div>
        </form>
      {/if}
    </div>
  </div>
{/if}
