<script lang="ts">
  import { ArrowLeft, ArrowRight, CheckCheck, Copy, Play, RefreshCw, RotateCw, Send, Trash2, Wand2, X } from 'lucide-svelte'
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { onDestroy, onMount } from 'svelte'

  import { postings as postingsApi, projects as projectsApi, wpSites as wpSitesApi, proxies as proxiesApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import DropdownMenu from '$lib/components/ui/DropdownMenu.svelte'
  import { runModeLabel } from '$lib/runLabels'
  import { prettyUrl } from '$lib/url'
  import type {
    PostingRun,
    PostingRunStatus,
    RunProgress,
    TextItem,
    TextItemStatus,
  } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let runId = $derived(Number(page.params.id))

  let run = $state<PostingRun | null>(null)
  let progress = $state<RunProgress | null>(null)
  let items = $state<TextItem[]>([])
  // Пагинация айтемов (cursor, MAX_LIMIT=200). loadedPages — сколько страниц
  // подгружено; живой реролл перечитывает их ВСЕ (сохраняя глубину просмотра и
  // свежие статусы). next_cursor стабилен (sort_key ≈ id).
  const PER_PAGE = 200
  let nextCursor = $state<string | null>(null)
  let hasMore = $state(false)
  let loadingMore = $state(false)
  let loadedPages = $state(1)
  let loading = $state(true)

  // Сводка доменов needs_review-задач прогона (массовый резолв «по домену»).
  // Пустой список → баннера нет; после привязки домен исчезает сам.
  let nrDomains = $state<{ domain: string; count: number; is_project_domain: boolean }[]>([])
  let nrBusy = $state('')  // 'resolve:<domain>' | 'add:<domain>'

  // 'in_progress' — виртуальный фильтр карточки Pending (pending + posting in-flight)
  let filterStatus = $state<TextItemStatus | 'all' | 'in_progress'>('all')
  let busyAction = $state<'pause' | 'resume' | 'cancel' | 'retry' | 'delete' | null>(null)

  // ─── Inline-edit max_posts_per_site (лимит повторов сайта в задаче) ──
  let editMpps = $state(false)
  let mppsValue = $state(1)
  let mppsBusy = $state(false)
  function startEditMpps() {
    if (!run) return
    mppsValue = run.max_posts_per_site
    editMpps = true
  }
  async function saveMpps() {
    if (!run || mppsBusy) return
    mppsBusy = true
    try {
      run = await postingsApi.update(runId, { max_posts_per_site: mppsValue })
      editMpps = false
      showToast('success', `Лимит сайта = ${run.max_posts_per_site}. Воркер учтёт live; для добора нажми Retry failed / Resume.`)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      mppsBusy = false
    }
  }

  // ─── Edit run params (до старта + остановленные: см. canEditParams) ──
  let editOpen = $state(false)
  let editBusy = $state(false)
  let ePriority = $state<'low' | 'normal' | 'high'>('normal')
  let eMethod = $state<'auto' | 'xmlrpc_only' | 'admin_only'>('auto')
  let eVerify = $state<'mark' | 'auto'>('mark')
  let eSchedFor = $state('')
  let eSpread = $state(0)
  let ePubFrom = $state('')
  let ePubTo = $state('')
  const editToday = new Date().toISOString().slice(0, 10)
  let eWindowInvalid = $derived.by(() => {
    const a = ePubFrom, b = ePubTo
    if (!a && !b) return false
    if (!a || !b) return true
    return a > b
  })
  let eWindowFuture = $derived((!!ePubFrom && ePubFrom > editToday) || (!!ePubTo && ePubTo > editToday))
  // Пул доступов + max posts/site — редактируемые (по аналогии с формой создания).
  let ePoolMode = $state<'all' | 'tags' | 'domains'>('all')
  let eSiteTags = $state<string[]>([])
  let eSiteLangs = $state('')
  let eSiteTlds = $state('')
  let eSiteDomains = $state('')
  let eMaxPosts = $state(1)
  let ePoolFallback = $state(false)  // добить по полному пулу при исчерпании фильтра
  let eAvailableTags = $state<string[]>([])
  let eTagSearch = $state('')
  const E_TAG_CAP = 40
  let eFilteredTags = $derived.by(() => {
    const q = eTagSearch.trim().toLowerCase()
    return q ? eAvailableTags.filter((t) => t.toLowerCase().includes(q)) : eAvailableTags
  })
  let eTagResultsAll = $derived(eFilteredTags.filter((t) => !eSiteTags.includes(t)))
  let eTagResults = $derived(eTagResultsAll.slice(0, E_TAG_CAP))
  let eTagResultsMore = $derived(Math.max(0, eTagResultsAll.length - E_TAG_CAP))
  function eToggleTag(tag: string) {
    eSiteTags = eSiteTags.includes(tag) ? eSiteTags.filter((t) => t !== tag) : [...eSiteTags, tag]
  }
  async function loadEditTags() {
    try {
      eAvailableTags = await wpSitesApi.credentialTags()
    } catch {
      eAvailableTags = []
    }
  }
  // Прокси-пул для селектора (как в форме создания).
  let eProxySelector = $state('direct')
  let ePoolStats = $state<{ all_active: number; providers: Record<string, number> }>({
    all_active: 0,
    providers: {},
  })
  async function loadEditPoolStats() {
    try {
      ePoolStats = await proxiesApi.pools()
    } catch {
      ePoolStats = { all_active: 0, providers: {} }
    }
  }
  // Раскрывающиеся секции модала + их сводки (как в форме создания).
  let secPoolOpen = $state(true)
  let secSchedOpen = $state(false)
  let secPostOpen = $state(false)
  let ePoolSummary = $derived(
    ePoolMode === 'tags'
      ? `по тегам: ${eSiteTags.length || '—'}`
      : ePoolMode === 'domains'
        ? 'свой список доменов'
        : eSiteLangs.trim() || eSiteTlds.trim()
          ? [eSiteLangs.trim() && `яз: ${eSiteLangs.trim()}`, eSiteTlds.trim() && `tld: ${eSiteTlds.trim()}`]
              .filter(Boolean)
              .join(' · ')
          : 'весь пул',
  )
  let eSchedSummary = $derived(
    [
      eSchedFor ? 'по расписанию' : 'сразу',
      ePubFrom && ePubTo ? 'своё окно' : 'станд. окно',
      eSpread > 0 ? `drip ${eSpread}д` : 'без drip',
    ].join(' · '),
  )
  let ePostSummary = $derived(
    [
      ePriority,
      `${eMaxPosts || 1}/сайт`,
      eProxySelector === 'direct' ? 'без прокси' : eProxySelector === 'all' ? 'все прокси' : eProxySelector,
      eMethod,
    ].join(' · '),
  )
  function isoToLocalInput(iso: string): string {
    const d = new Date(iso)
    const p = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
  }
  function openEdit() {
    if (!run) return
    ePriority = (run.priority ?? 'normal') as 'low' | 'normal' | 'high'
    eMethod = (run.posting_method ?? 'auto') as 'auto' | 'xmlrpc_only' | 'admin_only'
    eVerify = (run.post_verify ?? 'mark') as 'mark' | 'auto'
    eSchedFor = run.scheduled_for ? isoToLocalInput(run.scheduled_for) : ''
    eSpread = run.spread_days ?? 0
    ePubFrom = run.publish_from ?? ''
    ePubTo = run.publish_to ?? ''
    // Пул + max — префилл из текущих значений рана.
    eMaxPosts = run.max_posts_per_site ?? 1
    eSiteLangs = (run.site_langs ?? []).join(',')
    eSiteTlds = (run.site_tlds ?? []).join(',')
    eSiteTags = [...(run.site_tags ?? [])]
    eSiteDomains = ''
    eTagSearch = ''
    ePoolMode =
      run.site_tags && run.site_tags.length
        ? 'tags'
        : run.site_domains_count || run.site_domains_file
          ? 'domains'
          : 'all'
    eProxySelector = run.proxy_selector ?? 'direct'
    ePoolFallback = run.pool_fallback ?? false
    // Пул открыт по умолчанию (главное при need_more_admins), остальные свёрнуты.
    secPoolOpen = true
    secSchedOpen = false
    secPostOpen = false
    void loadEditTags()
    void loadEditPoolStats()
    editOpen = true
  }
  async function saveEdit() {
    if (!run || editBusy || eWindowInvalid || eWindowFuture) return
    editBusy = true
    try {
      // domains: пустое поле не отправляем — иначе затрём существующий список
      // (его текст в ответе рана не приходит, только count). Смена на all/tags
      // очищает домены осознанно (site_domains: null).
      const poolExtra =
        ePoolMode === 'domains'
          ? eSiteDomains.trim()
            ? { site_domains: eSiteDomains.trim() }
            : {}
          : { site_domains: null }
      run = await postingsApi.update(runId, {
        priority: ePriority,
        spread_days: eSpread || 0,
        posting_method: eMethod,
        post_verify: eVerify,
        scheduled_for: eSchedFor ? new Date(eSchedFor).toISOString() : null,
        publish_from: ePubFrom || null,
        publish_to: ePubTo || null,
        max_posts_per_site: eMaxPosts || 1,
        proxy_selector: eProxySelector,
        pool_fallback: ePoolFallback,
        site_langs: eSiteLangs.trim() || null,
        site_tlds: eSiteTlds.trim() || null,
        site_tags: ePoolMode === 'tags' ? eSiteTags.join(',') || null : null,
        ...poolExtra,
      })
      editOpen = false
      showToast('success', 'Параметры обновлены')
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { editBusy = false }
  }

  // SSE + fallback polling
  let eventSource: EventSource | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let sseConnected = $state(false)

  // Debounced reload text_items на каждый progress-tick — без debounce будем
  // дёргать /text-items на каждый из 1000 постов и нагружать API.
  let itemsReloadTimer: ReturnType<typeof setTimeout> | null = null
  function scheduleItemsReload() {
    if (itemsReloadTimer) clearTimeout(itemsReloadTimer)
    itemsReloadTimer = setTimeout(() => loadItems(), 1500)
  }

  // ─── Loading ───────────────────────────────────────────────────────

  async function loadRun() {
    try {
      run = await postingsApi.get(runId)
      void loadProjectDomains()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // Домены, добавленные в проект рана — по ним делаем target_domain кликабельным
  // (ссылка на страницу домена). Нормализуем в lowercase без www для матча.
  let projectDomains = $state<Set<string>>(new Set())
  function normDomain(d: string): string {
    return d.trim().toLowerCase().replace(/^www\./, '')
  }
  async function loadProjectDomains() {
    if (!run) return
    try {
      const doms = await projectsApi.listDomains(run.project.id)
      projectDomains = new Set(doms.map((d) => normDomain(d.domain)))
    } catch {
      // не критично — просто домен останется некликабельным
    }
  }

  async function loadProgress() {
    try {
      progress = await postingsApi.progress(runId)
    } catch {
      progress = null
    }
  }

  // Токен против гонок: во время прогона loadItems зовётся из SSE + поллинга;
  // устаревший (медленный) ответ не должен перетирать свежий (posted → posting флик).
  let itemsReqToken = 0
  function currentStatusParam(): string | undefined {
    return filterStatus === 'all' ? undefined
      : filterStatus === 'in_progress' ? 'pending,posting'
      : filterStatus
  }

  async function loadItems() {
    const token = ++itemsReqToken
    const statusParam = currentStatusParam()
    try {
      // Перечитываем все уже загруженные страницы (cursor) — реролл во время
      // прогона обновляет статусы по всей глубине, не сбрасывая просмотр.
      const acc: TextItem[] = []
      let cursor: string | undefined
      let more = false
      let last: string | null = null
      for (let p = 0; p < loadedPages; p++) {
        const res = await postingsApi.textItems(runId, { limit: PER_PAGE, status: statusParam, cursor })
        if (token !== itemsReqToken) return  // пришёл устаревший ответ — игнорируем
        acc.push(...res.items)
        more = res.has_more
        last = res.next_cursor
        if (!res.has_more || !res.next_cursor) break
        cursor = res.next_cursor
      }
      items = acc
      nextCursor = last
      hasMore = more
    } catch (e) {
      if (token === itemsReqToken) showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function loadMore() {
    if (!hasMore || !nextCursor || loadingMore) return
    loadingMore = true
    const token = itemsReqToken
    try {
      const res = await postingsApi.textItems(runId, { limit: PER_PAGE, status: currentStatusParam(), cursor: nextCursor })
      if (token !== itemsReqToken) return  // пока грузили — был реролл, игнорируем
      items = [...items, ...res.items]
      nextCursor = res.next_cursor
      hasMore = res.has_more
      loadedPages += 1
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loadingMore = false
    }
  }

  async function loadNrDomains() {
    try {
      nrDomains = await postingsApi.needsReviewDomains(runId)
    } catch {
      nrDomains = []
    }
  }

  // Привязать выбранный домен ко ВСЕМ needs_review прогона (каждой своя ссылка).
  async function nrBulkResolve(domain: string) {
    if (nrBusy) return
    nrBusy = `resolve:${domain}`
    try {
      const res = await postingsApi.resolveBulk(runId, domain)
      showToast('success', `${domain}: привязано ${res.resolved}, пропущено ${res.skipped} → в очередь`)
      await Promise.all([loadNrDomains(), loadItems(), loadProgress(), loadRun()])
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { nrBusy = '' }
  }

  // Добавить домен в проект → авто-резолв всех needs_review с ним (и будущих).
  async function nrAddDomain(domain: string) {
    if (nrBusy) return
    nrBusy = `add:${domain}`
    try {
      const res = await postingsApi.addProjectDomain(runId, domain)
      showToast('success', `${res.domain} в проекте — авто-резолв (${res.auto_resolved_runs} прогон.)`)
      await Promise.all([loadNrDomains(), loadItems(), loadProgress(), loadRun()])
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { nrBusy = '' }
  }

  // ── Целевые домены прогона, которых ещё нет в проекте (явные ссылки CSV) ──
  // Не добавляются автоматом → предлагаем добавить вручную, под раскрывающейся
  // формой (доменов может быть много).
  let missingDomains = $state<{ domain: string; count: number }[]>([])
  let missingBusy = $state('')
  let showMissing = $state(false)

  async function loadMissingDomains() {
    try {
      missingDomains = await postingsApi.missingProjectDomains(runId)
    } catch {
      missingDomains = []
    }
  }

  async function missingAddDomain(domain: string) {
    if (missingBusy) return
    missingBusy = `add:${domain}`
    try {
      await postingsApi.addProjectDomain(runId, domain)
      showToast('success', `${domain} → в проекте`)
      await loadMissingDomains()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { missingBusy = '' }
  }

  async function missingAddAll() {
    if (missingBusy) return
    missingBusy = 'all'
    try {
      const res = await postingsApi.addProjectDomains(runId, missingDomains.map((d) => d.domain))
      showToast('success', `Добавлено в проект: ${res.added.length}`)
      await loadMissingDomains()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { missingBusy = '' }
  }

  async function refresh(initial = false) {
    if (initial) loading = true
    await Promise.all([loadRun(), loadProgress(), loadItems(), loadNrDomains(), loadMissingDomains()])
    if (initial) loading = false
  }

  function isActiveStatus(s: PostingRunStatus | undefined): boolean {
    return s === 'unpacking' || s === 'queued' || s === 'running' || s === 'paused' || s === 'scheduled'
  }

  let genTextsBusy = $state(false)
  async function doGenerateTexts() {
    if (!run) return
    genTextsBusy = true
    try {
      await postingsApi.generateTexts(runId)
      showToast('success', 'Генерация текстов запущена (фоном). Постинг — кнопкой Start, когда тексты готовы.')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      genTextsBusy = false
    }
  }

  let fillSpinsBusy = $state(false)
  async function doFillSpins() {
    if (!run) return
    fillSpinsBusy = true
    try {
      await postingsApi.fillSpins(runId)
      showToast('success', 'Заполнение спинов запущено (фоном). Спины появятся в таблице построчно — можно проверить перед постингом.')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      fillSpinsBusy = false
    }
  }

  async function doStart() {
    if (!run) return
    const msg = run.status === 'scheduled'
      ? `Запустить run #${runId} НЕМЕДЛЕННО (не дожидаясь scheduled_for ${run.scheduled_for ? new Date(run.scheduled_for).toLocaleString() : ''})?`
      : run.status === 'unpacking'
        ? `Запустить постинг параллельно с генерацией? Будут постаться тексты по мере готовности, генерация продолжится.`
        : `Запустить run #${runId}? Постинг ${run.total_texts} текстов начнётся немедленно.`
    if (!confirm(msg)) return
    try {
      await postingsApi.start(runId)
      showToast('success', 'Run started')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function doRestart() {
    if (!run) return
    const remaining = run.total_texts - run.posted_count
    if (!confirm(
      `Перезапустить run #${runId}?\n\n` +
      `${run.posted_count} уже опубликованных текстов останутся posted, ` +
      `${remaining} оставшихся (failed/posting/skipped/pending) сбросятся в pending и попадут в работу снова.`,
    )) return
    try {
      const res = await postingsApi.restart(runId)
      showToast('success', `Restarted: ${res.items_reset} items reset to pending`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // SSE: подписка на live-события прогресса. На UI обновляем счётчики локально
  // без запроса к API. text_items перезагружаем debounce-ом.
  function startSSE() {
    if (eventSource) return
    eventSource = new EventSource(`/admin/api/postings/${runId}/events`)

    eventSource.addEventListener('snapshot', (e) => {
      sseConnected = true
      try {
        const d = JSON.parse(e.data)
        progress = {
          total: d.total ?? 0,
          pending: d.pending ?? 0,
          generating: d.generating ?? progress?.generating ?? 0,
          posting: d.posting ?? 0,
          posted: d.posted ?? 0,
          failed: d.failed ?? 0,
          skipped: d.skipped ?? 0,
          needs_review: d.needs_review ?? progress?.needs_review ?? 0,
          generated: d.generated ?? progress?.generated ?? 0,
        }
      } catch { /* ignore */ }
    })

    eventSource.addEventListener('progress', (e) => {
      try {
        const d = JSON.parse(e.data)
        if (run) {
          run = {
            ...run,
            posted_count: d.posted ?? run.posted_count,
            failed_count: d.failed ?? run.failed_count,
            skipped_count: d.skipped ?? run.skipped_count,
            total_texts: d.total ?? run.total_texts,
            status: d.status ?? run.status,
          }
        }
        if (progress) {
          const total = d.total ?? progress.total
          const pending = Math.max(0, total - (d.posted ?? 0) - (d.failed ?? 0) - (d.skipped ?? 0) - (progress.posting ?? 0))
          progress = {
            ...progress,
            posted: d.posted ?? progress.posted,
            failed: d.failed ?? progress.failed,
            skipped: d.skipped ?? progress.skipped,
            total,
            pending,
          }
        }
        scheduleItemsReload()
      } catch { /* ignore */ }
    })

    eventSource.addEventListener('status', (e) => {
      try {
        const d = JSON.parse(e.data)
        if (run && d.status) {
          run = { ...run, status: d.status as PostingRunStatus }
        }
        // На status-change — полный re-fetch (заехало started_at/finished_at и пр.)
        loadRun()
        loadProgress()
        scheduleItemsReload()
      } catch { /* ignore */ }
    })

    eventSource.onerror = () => {
      // Браузер сам пытается reconnect. Если совсем сломалось — fallback polling
      // (pollTimer уже запущен).
      sseConnected = false
    }
  }

  function stopSSE() {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
  }

  // Fallback polling + поддержка dual-бара: progress-событие SSE не несёт
  // `generated` (счётчик генерации), поэтому для активного рана периодически
  // подтягиваем progress даже при живом SSE (постинг при этом — live по SSE).
  function tickPoll() {
    if (!run || !isActiveStatus(run.status)) return
    if (!sseConnected) {
      refresh(false)
    } else {
      loadProgress()
      // Фаза генерации/расшивки (UNPACKING): SSE не шлёт смену статусов айтемов —
      // подтягиваем строки, чтобы было видно построчное наполнение текстов/спинов.
      if (run.status === 'unpacking') loadItems()
    }
  }

  onMount(async () => {
    await refresh(true)
    startSSE()
    pollTimer = setInterval(tickPoll, 10000)
    // 1-сек тик для elapsed/ETA — обновляем только пока ран активен.
    etaTimer = setInterval(() => {
      if (isActiveStatus(run?.status)) nowMs = Date.now()
    }, 1000)
  })
  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer)
    if (etaTimer) clearInterval(etaTimer)
    if (itemsReloadTimer) clearTimeout(itemsReloadTimer)
    if (linkCheckTimer) clearInterval(linkCheckTimer)
    stopSSE()
  })

  // ─── Permissions ───────────────────────────────────────────────────

  let canManage = $derived.by(() => {
    const u = $currentUser
    if (!u || !run) return false
    if (u.is_super_admin) return true
    // Минимум — creator. Точнее проверяет backend; UI не блокирует кнопки,
    // если запрос вернёт 403, покажем toast.
    return run.creator?.id === u.id
  })

  // ─── Actions ──────────────────────────────────────────────────────

  async function doAction(kind: 'pause' | 'resume' | 'cancel' | 'retry' | 'delete') {
    busyAction = kind
    try {
      if (kind === 'pause') await postingsApi.pause(runId)
      else if (kind === 'resume') await postingsApi.resume(runId)
      else if (kind === 'cancel') {
        if (!confirm('Отменить прогон? Все pending тексты не будут опубликованы.')) {
          busyAction = null
          return
        }
        await postingsApi.cancel(runId)
      } else if (kind === 'retry') {
        const res = await postingsApi.retryFailed(runId)
        showToast('success', `Retried ${res.retried} failed item(s)`)
      } else if (kind === 'delete') {
        if (!confirm('Удалить прогон? Он исчезнет из списков (БД-история сохранится). Если активен — будет отменён.')) {
          busyAction = null
          return
        }
        await postingsApi.remove(runId)
        showToast('success', 'Run deleted')
        goto('/runs')
        return
      }
      await refresh(false)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      busyAction = null
    }
  }

  function changeStatusFilter(s: TextItemStatus | 'all' | 'in_progress') {
    filterStatus = s
    loadedPages = 1
    nextCursor = null
    hasMore = false
    loadItems()
  }

  // ─── Сортировка колонок таблицы текстов ──
  // Клиентская сортировка идёт по ЗАГРУЖЕННЫМ айтемам. Чтобы сортировка была по
  // ВСЕМ (а не только по первой странице), при клике догружаем остальные страницы
  // текущего фильтра. Для фильтра по статусу (напр. needs_review) их обычно мало.
  let sortKey = $state<string | null>(null)
  let sortDir = $state<'asc' | 'desc'>('asc')
  let loadingAll = $state(false)
  async function loadAllRemaining() {
    if (loadingAll) return
    loadingAll = true
    const token = itemsReqToken
    try {
      while (hasMore && nextCursor) {
        const res = await postingsApi.textItems(runId, { limit: PER_PAGE, status: currentStatusParam(), cursor: nextCursor })
        if (token !== itemsReqToken) return
        items = [...items, ...res.items]
        nextCursor = res.next_cursor
        hasMore = res.has_more
        loadedPages += 1
      }
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loadingAll = false
    }
  }
  async function toggleSort(key: string) {
    if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc'
    else { sortKey = key; sortDir = 'asc' }
    if (hasMore) await loadAllRemaining()  // сортируем по всем, не по первой странице
  }
  function itemSortVal(it: TextItem, key: string): string | number {
    switch (key) {
      case 'id': return it.id
      case 'link': return (it.link_url ?? '').toLowerCase()
      case 'anchor': return (it.link_anchor ?? '').toLowerCase()
      case 'text': return (it.title ?? '').toLowerCase()
      case 'status': return it.status
      case 'posted': return it.posted_at ? new Date(it.posted_at).getTime() : 0
      default: return 0
    }
  }
  let sortedItems = $derived.by(() => {
    if (!sortKey) return items
    const k = sortKey, dir = sortDir === 'asc' ? 1 : -1
    return [...items].sort((a, b) => {
      const va = itemSortVal(a, k), vb = itemSortVal(b, k)
      return va < vb ? -dir : va > vb ? dir : 0
    })
  })

  // Контент-фильтр (только gen-задачи, клиентский по загруженным): по наличию
  // текста и спинам. Спин-айтемы помечены original_filename='(спин)'.
  let contentFilter = $state<'all' | 'with_text' | 'no_text' | 'spin'>('all')
  function isSpin(it: TextItem): boolean {
    return it.original_filename === '(спин)'
  }
  let displayedItems = $derived.by(() => {
    if (contentFilter === 'all') return sortedItems
    return sortedItems.filter((it) =>
      contentFilter === 'with_text' ? it.text_id != null
      : contentFilter === 'no_text' ? it.text_id == null
      : isSpin(it))
  })

  const isLinkRun = $derived(
    run?.task_type === 'sitewide_link' || run?.task_type === 'homepage_link',
  )

  // Какой пул доступов использует ран (фильтр из gen_params) — для инфо
  let poolLabel = $derived.by(() => {
    if (!run) return '—'
    const parts: string[] = []
    if (run.site_tags?.length) parts.push(`теги: ${run.site_tags.join(', ')}`)
    if (run.site_domains_count) parts.push(`свой список: ${run.site_domains_count} дом.`)
    else if (run.site_domains_file) parts.push('свой список (файл)')
    if (run.site_langs?.length) parts.push(`язык: ${run.site_langs.join(',')}`)
    if (run.site_tlds?.length) parts.push(`TLD: ${run.site_tlds.join(',')}`)
    return parts.length ? parts.join(' · ') : 'Весь пул'
  })

  let removingItem = $state<number | null>(null)
  async function removeLink(itemId: number) {
    if (!confirm('Снять размещённую сквозную ссылку с этого сайта?')) return
    removingItem = itemId
    try {
      const res = await postingsApi.removeLink(runId, itemId)
      if (res.status === 'removed') showToast('success', 'Ссылка снята')
      else showToast('error', `Не удалось снять (${res.status})`)
      await loadItems()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      removingItem = null
    }
  }

  // ─── Пер-айтем действия (generate/regenerate/post/repost) ──────────
  const isGenRun = $derived(run?.content_source === 'csv_campaign')
  let itemBusy = $state<number | null>(null)

  // Поллим конкретный айтем, пока он не «осядет»: для генерации — пока не уйдёт
  // из generating; для постинга — пока не достигнет терминального статуса (увидев
  // перед этим transient). Так строка обновляется live (статус → готовый текст/URL).
  async function pollItemSettled(itemId: number, action: string) {
    const transient: string[] = (action === 'generate' || action === 'regenerate')
      ? ['generating'] : ['pending', 'posting']
    let sawTransient = false
    for (let i = 0; i < 25; i++) {
      const it = items.find((x) => x.id === itemId)
      if (it && transient.includes(it.status)) sawTransient = true
      if (it && sawTransient && !transient.includes(it.status)) return // осел
      await new Promise((r) => setTimeout(r, 1500))
      await loadItems()
    }
  }

  async function itemAction(itemId: number, action: 'generate' | 'regenerate' | 'post' | 'repost') {
    if (action === 'repost' &&
        !confirm('Перезапостить на другой сайт? Текущий сайт исключим, это съест ещё один слот сайта.')) return
    itemBusy = itemId
    try {
      const fn = {
        generate: postingsApi.generateItem, regenerate: postingsApi.regenerateItem,
        post: postingsApi.postItem, repost: postingsApi.repostItem,
      }[action]
      await fn(runId, itemId)
      showToast('success', {
        generate: 'Генерация запущена', regenerate: 'Перегенерация запущена',
        post: 'Постинг запущен', repost: 'Repost запущен',
      }[action])
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
      itemBusy = null
      return
    }
    itemBusy = null
    await loadItems()                       // показать claim (generating) сразу
    await pollItemSettled(itemId, action)   // обновлять строку до завершения
  }

  async function deleteItem(item: TextItem) {
    if (!confirm(`Удалить айтем #${item.id} (${item.status}) из задачи? Это необратимо.`)) return
    itemBusy = item.id
    try {
      const res = await postingsApi.deleteTextItem(runId, item.id)
      items = items.filter((i) => i.id !== item.id)
      showToast('success', res.run_status === 'done'
        ? 'Айтем удалён. Задача завершена (done).'
        : `Айтем #${item.id} удалён`)
      await Promise.all([loadProgress(), loadRun()])
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      itemBusy = null
    }
  }

  // ─── Display helpers ───────────────────────────────────────────────

  function runStatusClass(s: PostingRunStatus): string {
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

  function itemStatusClass(s: TextItemStatus): string {
    switch (s) {
      case 'pending':
        return 'bg-slate-100 text-slate-600'
      case 'generating':
        return 'bg-orange-100 text-orange-700'
      case 'posting':
        return 'bg-brand-100 text-brand-700'
      case 'posted':
        return 'bg-emerald-100 text-emerald-700'
      case 'failed':
        return 'bg-red-100 text-red-700'
      case 'skipped':
        return 'bg-amber-100 text-amber-700'
      case 'needs_review':
        return 'bg-orange-100 text-orange-700'
      default:
        return 'bg-slate-100 text-slate-500'
    }
  }

  function pct(n: number, total: number): number {
    if (!total) return 0
    return Math.round((n * 100) / total)
  }

  function fmtBytes(n: number): string {
    if (n < 1024) return `${n} B`
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
    return `${(n / 1024 / 1024).toFixed(2)} MB`
  }

  function fmtDuration(start: string | null, end: string | null): string {
    if (!start) return '—'
    const s = new Date(start).getTime()
    const e = (end ? new Date(end) : new Date()).getTime()
    const sec = Math.max(0, Math.floor((e - s) / 1000))
    if (sec < 60) return `${sec}s`
    const m = Math.floor(sec / 60)
    if (m < 60) return `${m}m ${sec % 60}s`
    const h = Math.floor(m / 60)
    return `${h}h ${m % 60}m`
  }

  function postingMethodLabel(m?: string | null): string {
    switch (m) {
      case 'xmlrpc_only': return 'XML-RPC only'
      case 'admin_only': return 'wp-admin only'
      default: return 'Auto (XML-RPC → wp-admin)'
    }
  }

  // Кнопки которые имеют смысл в текущем статусе
  let canPause = $derived(run?.status === 'running' || run?.status === 'queued')
  let canResume = $derived(
    run?.status === 'paused' || run?.status === 'interrupted' || run?.pause_requested === true,
  )
  let canCancel = $derived(
    !!run && !['done', 'cancelled', 'failed'].includes(run.status),
  )
  let canRetry = $derived(!!progress && progress.failed > 0)
  // Правка параметров: до старта + в остановленных перезапускаемых статусах
  // (расширить пул/окно и нажать Restart). Согласовано с бэкендом.
  let canEditParams = $derived(
    !!run &&
      ['ready', 'scheduled', 'need_more_admins', 'interrupted', 'cancelled', 'failed'].includes(
        run.status,
      ),
  )
  let canDownload = $derived(!!run && run.total_texts > 0)

  // ─── ETA + скорость постинга (как в батчах) ────────────────────────
  // 1-сек тик пересчитывает elapsed/ETA только пока ран активен.
  let nowMs = $state(Date.now())
  let etaTimer: ReturnType<typeof setInterval> | null = null

  function fmtMs(ms: number): string {
    if (!isFinite(ms) || ms < 0) return '—'
    const s = Math.round(ms / 1000)
    if (s < 60) return `${s}s`
    const m = Math.floor(s / 60); const sr = s % 60
    if (m < 60) return sr > 0 ? `${m}m ${sr}s` : `${m}m`
    const h = Math.floor(m / 60); const mr = m % 60
    return mr > 0 ? `${h}h ${mr}m` : `${h}h`
  }

  let elapsedMs = $derived(
    run?.started_at ? nowMs - new Date(run.started_at).getTime() : 0,
  )
  // done = терминальные айтемы (для ETA по скорости обработки)
  let doneSoFar = $derived(
    (progress?.posted ?? 0) + (progress?.failed ?? 0) + (progress?.skipped ?? 0),
  )
  // скорость именно ПОСТИНГА — успешные посты в минуту
  let postsPerMin = $derived(
    elapsedMs > 1000 && (progress?.posted ?? 0) > 0
      ? (progress!.posted) / (elapsedMs / 60000)
      : 0,
  )
  let procRatePerSec = $derived(
    elapsedMs > 1000 && doneSoFar > 0 ? doneSoFar / (elapsedMs / 1000) : 0,
  )
  let etaMs = $derived(
    progress && procRatePerSec > 0
      ? Math.max(0, (progress.total - doneSoFar) / procRatePerSec) * 1000
      : Infinity,
  )

  // ─── Перепроверка проставленных ссылок (link-check) ────────────────
  let validating = $state(false)
  let linkCheckTimer: ReturnType<typeof setInterval> | null = null
  let linkCheckRunning = $derived(
    run?.link_check_status === 'running' || run?.link_check_status === 'queued',
  )
  let canValidateLinks = $derived(
    !!run && run.status === 'done' && run.posted_count > 0 && !linkCheckRunning,
  )

  async function doValidateLinks() {
    if (!run || validating || linkCheckRunning) return
    validating = true
    try {
      const res = await postingsApi.validateLinks(runId)
      showToast('success', `Проверка ссылок запущена: ${res.total} шт.`)
      await loadRun()
      // Быстрый поллинг прогресса проверки, пока бежит (общий poll — раз в 10с).
      if (linkCheckTimer) clearInterval(linkCheckTimer)
      linkCheckTimer = setInterval(async () => {
        await loadRun()
        if (run?.link_check_status !== 'running' && run?.link_check_status !== 'queued') {
          if (linkCheckTimer) {
            clearInterval(linkCheckTimer)
            linkCheckTimer = null
          }
          loadItems() // обновить отметки ✓/✗ в таблице
        }
      }, 3000)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      validating = false
    }
  }
</script>

<div class="space-y-6">
  <!-- Header -->
  <div>
    <a href="/runs" class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Runs</a>
    {#if loading}
      <h1 class="mt-1 text-2xl font-semibold text-slate-900">Loading…</h1>
    {:else if run}
      <div class="mt-1 flex items-center gap-3">
        <h1 class="text-2xl font-semibold text-slate-900">{run.name}</h1>
        <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {runStatusClass(run.status)}">
          {run.status.replace('_', ' ')}
        </span>
        <span class="rounded-full bg-indigo-100 px-2 py-0.5 text-[11px] font-medium text-indigo-700">
          {runModeLabel(run)}
        </span>
        {#if run.pause_requested && run.status === 'running'}
          <span class="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium uppercase text-amber-700">pause requested</span>
        {/if}
        {#if run.proxy_fallback_direct}
          <span class="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium uppercase text-amber-700"
                title="На старте прогона пул прокси оказался в основном недоступен (проверьте оплату/статус). Постинг идёт напрямую с IP сервера — CF-сайты и admin-размещение будут пропускаться.">
            ⚠ прокси недоступны — direct
          </span>
        {/if}
      </div>
      {#if run.proxy_fallback_direct}
        <div class="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          ⚠ Прогон запущен <strong>без прокси</strong>: на старте пул оказался в основном недоступен (частая причина — прокси не оплачены).
          Постинг идёт напрямую с IP сервера, поэтому CF-защищённые сайты и размещение через admin будут пропускаться.
          Проверьте прокси в разделе <a href="/proxies" class="underline">Proxies</a> и перезапустите прогон.
        </div>
      {/if}
      <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>Project: <a class="text-brand-600 hover:underline" href={`/projects/${run.project.id}`}>{run.project.name}</a></span>
        <span>Creator: <strong>@{run.creator?.username ?? '—'}</strong></span>
        <span>Priority: <strong class="uppercase">{run.priority}</strong></span>
        <span class="inline-flex items-center gap-1"
              title="Сколько раз один WP-сайт можно использовать в этой задаче. 1 = «1 сайт = 1 пост». Подними, чтобы добрать сайты из уже использованных.">
          Max posts/site:
          {#if editMpps}
            <input type="number" min="1" max="1000" bind:value={mppsValue}
                   class="w-16 rounded border border-slate-300 px-1 py-0.5 text-xs" />
            <button type="button" onclick={saveMpps} disabled={mppsBusy}
                    class="rounded bg-brand-600 px-1.5 py-0.5 text-[11px] font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
              {mppsBusy ? '…' : 'Save'}
            </button>
            <button type="button" onclick={() => (editMpps = false)}
                    class="text-[11px] text-slate-500 hover:text-slate-700">cancel</button>
          {:else}
            <strong>{run.max_posts_per_site}</strong>
            <button type="button" onclick={startEditMpps}
                    class="text-[11px] text-brand-600 hover:underline">изменить</button>
          {/if}
        </span>
        <span>Created: {new Date(run.created_at).toLocaleString()}</span>
        {#if run.scheduled_for}<span>Scheduled: {new Date(run.scheduled_for).toLocaleString()}</span>{/if}
        {#if run.started_at}<span>Started: {new Date(run.started_at).toLocaleString()}</span>{/if}
        {#if run.finished_at}<span>Finished: {new Date(run.finished_at).toLocaleString()}</span>{/if}
        <span>Duration: <strong>{fmtDuration(run.started_at, run.finished_at)}</strong></span>
        {#if run.publish_from && run.publish_to}
          <span>Publish window: {run.publish_from} <ArrowRight size={14} class="inline-block align-text-bottom" /> {run.publish_to}</span>
        {/if}
      </div>
    {/if}
  </div>

  {#if run}
    <div class="flex flex-wrap gap-2">
      {#if canManage}
        <!-- Manual gen-задача: пока есть несгенерированные — «Сгенерировать тексты» -->
        {#if isGenRun && run.run_mode === 'manual' && run.status === 'ready' && (run.gen_total ?? 0) > 0 && (run.gen_done ?? 0) < (run.gen_total ?? 0)}
          <button onclick={doGenerateTexts} disabled={genTextsBusy}
                  class="inline-flex items-center gap-1.5 rounded-md bg-orange-500 px-4 py-1.5 text-sm font-medium text-white hover:bg-orange-600 disabled:opacity-50"
                  title="Сгенерировать все тексты задачи (фоном). Можно и точечно — кнопками в таблице.">
            <Wand2 size={14} /> {genTextsBusy ? 'Запуск…' : 'Сгенерировать тексты'}
          </button>
        {/if}
        <!-- gen_per_row: расшить готовые оригиналы в спины (без старта постинга) -->
        {#if isGenRun && run.run_mode === 'manual' && run.status === 'ready' && (run.fillable_spins ?? 0) > 0}
          <button onclick={doFillSpins} disabled={fillSpinsBusy}
                  class="inline-flex items-center gap-1.5 rounded-md border border-orange-300 bg-white px-4 py-1.5 text-sm font-medium text-orange-600 hover:bg-orange-50 disabled:opacity-50"
                  title="Расшить готовые оригиналы в спин-варианты — заполнить все пустые тексты-спины. Без старта постинга: можно проверить спины перед публикацией.">
            <Copy size={14} /> {fillSpinsBusy ? 'Запуск…' : `Заполнить спины (${run.fillable_spins})`}
          </button>
        {/if}
        <!-- «Старт постинга»: для gen_per_post можно поверх идущей генерации
             (UNPACKING) — постинг забирает готовые тексты, генерация наполняет
             остальные параллельно. -->
        {#if run.status === 'ready' || run.status === 'scheduled' || (run.status === 'unpacking' && run.content_mode === 'gen_per_post')}
          <button onclick={doStart}
                  class="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700"
                  title={run.status === 'scheduled'
                    ? 'Запустить НЕМЕДЛЕННО, не дожидаясь scheduled_for'
                    : (run.status === 'unpacking'
                       ? 'Запустить постинг параллельно с генерацией — постится то, что уже сгенерировано'
                       : (isGenRun ? 'Сгенерирует недостающие тексты и запостит по мере готовности' : 'Запустить постинг'))}>
            <Play size={14} /> {isGenRun ? 'Старт постинга' : 'Start'}{run.status === 'unpacking' ? ' (параллельно)' : ''}
          </button>
        {/if}
        {#if run.status === 'failed' || run.status === 'interrupted' || run.status === 'cancelled' || run.status === 'need_more_admins' || (run.status === 'done' && run.failed_count > 0)}
          {@const remaining = run.total_texts - run.posted_count}
          <button onclick={doRestart}
                  class="inline-flex items-center gap-1.5 rounded-md bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-emerald-700"
                  title="Сбросить failed/posting/skipped/pending → pending и запустить заново. Posted останутся как есть.">
            <Play size={14} /> Restart ({remaining > 0 ? remaining : run.failed_count})
          </button>
        {/if}
        <button onclick={() => doAction('pause')} disabled={!canPause || busyAction !== null}
                class="rounded-md border border-amber-300 bg-amber-50 px-3 py-1.5 text-sm font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-40">
          {busyAction === 'pause' ? '…' : 'Pause'}
        </button>
        <button onclick={() => doAction('resume')} disabled={!canResume || busyAction !== null}
                class="rounded-md border border-brand-300 bg-brand-50 px-3 py-1.5 text-sm font-medium text-brand-800 hover:bg-brand-100 disabled:opacity-40">
          {busyAction === 'resume' ? '…' : 'Resume'}
        </button>
        <button onclick={() => doAction('cancel')} disabled={!canCancel || busyAction !== null}
                class="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-800 hover:bg-red-100 disabled:opacity-40">
          {busyAction === 'cancel' ? '…' : 'Stop'}
        </button>
        <button onclick={() => doAction('retry')} disabled={!canRetry || busyAction !== null}
                class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
          {busyAction === 'retry' ? '…' : `Retry failed${progress?.failed ? ` (${progress.failed})` : ''}`}
        </button>
        {#if canEditParams}
          <button onclick={openEdit} disabled={busyAction !== null}
                  class="rounded-md border border-brand-300 bg-brand-50 px-3 py-1.5 text-sm font-medium text-brand-700 hover:bg-brand-100 disabled:opacity-40"
                  title="Изменить параметры (пул/окно/метод). Для остановленных — поправь и нажми Restart.">
            Edit
          </button>
        {/if}
        <button onclick={() => doAction('delete')} disabled={busyAction !== null}
                class="rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-40"
                title="Архивировать run (soft-delete). Активный — отменится.">
          {busyAction === 'delete' ? '…' : 'Delete'}
        </button>
        <!-- Перепроверка проставленных ссылок — только после завершения постинга -->
        {#if run.status === 'done'}
          <button onclick={doValidateLinks}
                  disabled={!canValidateLinks || validating}
                  class="inline-flex items-center gap-1.5 rounded-md border border-violet-300 bg-violet-50 px-3 py-1.5 text-sm font-medium text-violet-800 hover:bg-violet-100 disabled:opacity-50"
                  title="Перепроверить уже-валидные бэклинки (фетч страниц постов). Идёт в общей очереди — видно на странице «Очередь».">
            {#if linkCheckRunning}
              <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-violet-500"></span>
              Проверка ссылок… {run.link_check_done}/{run.link_check_total}
            {:else}
              <CheckCheck size={14} /> Проверить ссылки
            {/if}
          </button>
          {#if run.link_check_status === 'done' && run.link_check_at}
            <span class="self-center text-xs text-violet-700"
                  title={`Последняя проверка: ${new Date(run.link_check_at).toLocaleString()}`}>
              валидных {run.link_check_valid}/{run.link_check_total}
            </span>
          {/if}
        {/if}
      {/if}
      <div class="ml-auto">
        <DropdownMenu
          label="⤓ Download"
          disabled={!canDownload}
          title={canDownload ? 'Скачать результаты прогона' : 'Дождись распаковки архива'}
          items={[
            {
              label: 'CSV',
              description: 'Универсальный, Excel/Numbers/Sheets',
              href: `/admin/api/postings/${runId}/result?format=csv`,
              download: `run-${runId}.csv`,
            },
            {
              label: 'CSV — только валидные',
              description: 'Лишь подтверждённые ссылки (link_verified ✓)',
              href: `/admin/api/postings/${runId}/result?format=csv&verified_only=true`,
              download: `run-${runId}-valid.csv`,
            },
            {
              label: 'XLSX',
              description: 'Excel native, posted_url как гиперссылка',
              href: `/admin/api/postings/${runId}/result?format=xlsx`,
              download: `run-${runId}.xlsx`,
            },
            {
              label: 'XLSX — только валидные',
              description: 'Лишь подтверждённые ссылки (link_verified ✓)',
              href: `/admin/api/postings/${runId}/result?format=xlsx&verified_only=true`,
              download: `run-${runId}-valid.xlsx`,
            },
            {
              label: 'JSON',
              description: 'Массив объектов, для скриптов/API',
              href: `/admin/api/postings/${runId}/result?format=json`,
              download: `run-${runId}.json`,
            },
            {
              label: 'TXT (zip)',
              description: 'Тексты архивом: 1 .txt = постированная версия',
              href: `/admin/api/postings/${runId}/result?format=txt`,
              download: `run-${runId}-texts.zip`,
            },
          ]}
        />
      </div>
    </div>
  {/if}

  <!-- Параметры задачи (для постинга: метод, старт, период, генерация) -->
  {#if run?.content_params?.error}
    <div class="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
      <span class="font-medium">Ошибка генерации:</span> {run.content_params.error}
    </div>
  {/if}

  {#if run}
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <h2 class="text-sm font-medium text-slate-700">Параметры</h2>
      <dl class="mt-2 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
        {#if isLinkRun}
          <div>
            <dt class="text-xs text-slate-400">Тип</dt>
            <dd class="text-slate-800">{run.task_type === 'homepage_link' ? 'Ссылка с главной' : 'Сквозная ссылка'}</dd>
          </div>
        {:else}
          <div>
            <dt class="text-xs text-slate-400">Метод постинга</dt>
            <dd class="text-slate-800">{postingMethodLabel(run.posting_method)}</dd>
          </div>
          <div>
            <dt class="text-xs text-slate-400">Валидация ссылки</dt>
            <dd class="text-slate-800">{run.post_verify === 'auto' ? 'Автовалидация (перепост до подтверждения)' : 'Отметка ✓/✗'}</dd>
          </div>
        {/if}
        <div>
          <dt class="text-xs text-slate-400">Старт</dt>
          <dd class="text-slate-800">{run.scheduled_for ? `Отложенный: ${new Date(run.scheduled_for).toLocaleString()}` : 'Сразу после готовности'}</dd>
        </div>
        <div>
          <dt class="text-xs text-slate-400">Период постинга</dt>
          <dd class="text-slate-800">{run.spread_days && run.spread_days > 0 ? `Размазан на ${run.spread_days} дн.` : 'Всё сразу'}</dd>
        </div>
        {#if run.publish_from && run.publish_to}
          <div>
            <dt class="text-xs text-slate-400">Окно публикации</dt>
            <dd class="text-slate-800">{run.publish_from} → {run.publish_to}</dd>
          </div>
        {/if}
        <div>
          <dt class="text-xs text-slate-400">Пул доступов</dt>
          <dd class="text-slate-800">{poolLabel}</dd>
        </div>
        {#if isLinkRun}
          <div>
            <dt class="text-xs text-slate-400">Макс. на сайт</dt>
            <dd class="text-slate-800">{run.max_posts_per_site}</dd>
          </div>
          <div>
            <dt class="text-xs text-slate-400">Авто-добор по пулу</dt>
            <dd class="text-slate-800">{run.pool_fallback ? 'да' : 'нет'}</dd>
          </div>
          {#if run.proxy_selector && run.proxy_selector !== 'direct'}
            <div>
              <dt class="text-xs text-slate-400">Прокси</dt>
              <dd class="text-slate-800">{run.proxy_selector}</dd>
            </div>
          {/if}
        {/if}
        {#if run.content_params}
          <div>
            <dt class="text-xs text-slate-400">Язык</dt>
            <dd class="text-slate-800">{run.content_params.language || '—'}</dd>
          </div>
          <div>
            <dt class="text-xs text-slate-400">AI-модель</dt>
            <dd class="text-slate-800">{run.content_params.model || '—'}</dd>
          </div>
          <div>
            <dt class="text-xs text-slate-400">Шаблон промпта</dt>
            <dd class="text-slate-800">{run.content_params.prompt || '— без шаблона —'}</dd>
          </div>
        {/if}
      </dl>
    </div>
  {/if}

  <!-- Progress card — одна полоска: зелёный=постинг, красный=генерация (для gen) -->
  {#if progress && run}
    {@const done = progress.posted + progress.failed + progress.skipped}
    {@const totalPct = pct(done, progress.total)}
    {@const postedPct = pct(progress.posted, progress.total)}
    {@const failedPct = pct(progress.failed, progress.total)}
    {@const skippedPct = pct(progress.skipped, progress.total)}
    {@const genPct = pct(progress.generated, progress.total)}
    {@const genAheadPct = Math.max(0, genPct - postedPct - failedPct)}
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <span class="text-sm font-medium text-slate-700">Progress</span>
          <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {runStatusClass(run.status)}">
            {run.status.replace('_', ' ')}
          </span>
        </div>
        <span class="text-sm font-semibold text-slate-700">{totalPct}%</span>
      </div>
      {#if run.status === 'running'}
        <div class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500">
          <span title="С момента старта постинга">{fmtMs(elapsedMs)} прошло</span>
          {#if isFinite(etaMs) && etaMs > 0 && done < progress.total}
            <span class="text-slate-300">·</span>
            <span class="text-slate-600" title="Примерно до конца (по скорости обработки)">~{fmtMs(etaMs)} осталось</span>
          {/if}
          {#if postsPerMin > 0}
            <span class="text-slate-300">·</span>
            <span title="Скорость постинга">{postsPerMin.toFixed(1)} постов/мин</span>
          {/if}
        </div>
      {/if}
      {#if isGenRun}
        <!-- Бар: зелёный=постинг · оранжевый=генерация (ждёт постинга) · красный=ошибки -->
        <div class="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
          <div class="h-full bg-emerald-500 transition-all" style="width: {postedPct}%"></div>
          <div class="h-full bg-red-500 transition-all" style="width: {failedPct}%"></div>
          <div class="h-full bg-orange-400 transition-all" style="width: {genAheadPct}%"></div>
        </div>
        <div class="mt-2 flex flex-wrap items-center justify-center gap-x-5 gap-y-1 text-xs">
          <span class="inline-flex items-center gap-1.5 text-slate-600">
            <span class="inline-block h-2 w-2 rounded-full bg-orange-400"></span>
            генерация <strong class="text-orange-600">{progress.generated}</strong>/{progress.total}
          </span>
          <span class="inline-flex items-center gap-1.5 text-slate-600">
            <span class="inline-block h-2 w-2 rounded-full bg-emerald-500"></span>
            постинг <strong class="text-emerald-600">{progress.posted}</strong>/{progress.total}
          </span>
          {#if progress.failed > 0}
            <span class="inline-flex items-center gap-1.5 text-slate-600">
              <span class="inline-block h-2 w-2 rounded-full bg-red-500"></span>
              ошибки <strong class="text-red-600">{progress.failed}</strong>
            </span>
          {/if}
        </div>
      {:else}
        <!-- Обычный стек: posted/failed/skipped -->
        <div class="mt-3 flex h-2 w-full overflow-hidden rounded-full bg-slate-100">
          <div class="h-full bg-emerald-500" style="width: {postedPct}%"></div>
          <div class="h-full bg-red-500" style="width: {failedPct}%"></div>
          <div class="h-full bg-amber-400" style="width: {skippedPct}%"></div>
        </div>
      {/if}
      <!-- Карточки кликабельны: фильтруют таблицу текстов по статусу (как теги) -->
      <div class="mt-4 grid grid-cols-2 gap-3 text-center sm:grid-cols-5">
        {#snippet statCard(key: TextItemStatus | 'all' | 'in_progress', value: number, label: string, color: string, sub = '')}
          <button type="button" onclick={() => changeStatusFilter(key)}
                  class="rounded-lg border p-2 transition hover:bg-slate-50"
                  class:border-brand-400={filterStatus === key}
                  class:bg-brand-50={filterStatus === key}
                  class:border-slate-200={filterStatus !== key}>
            <div class="text-2xl font-semibold {color}">{value}</div>
            <div class="text-[11px] uppercase tracking-wider text-slate-500">
              {label}{#if sub} · <span class="normal-case text-slate-400">{sub}</span>{/if}
            </div>
          </button>
        {/snippet}
        {@render statCard('all', progress.total, 'Total', 'text-slate-900')}
        {@render statCard('posted', progress.posted, 'Posted', 'text-emerald-600', progress.total ? `${postedPct}%` : '')}
        {@render statCard('failed', progress.failed, 'Failed', 'text-red-600', progress.total ? `${failedPct}%` : '')}
        {@render statCard('skipped', progress.skipped, 'Skipped', 'text-amber-600', progress.total ? `${skippedPct}%` : '')}
        {@render statCard('in_progress', progress.pending + progress.posting, 'Pending', 'text-brand-700', progress.posting > 0 ? `${progress.posting} in-flight` : '')}
      </div>
    </div>
  {/if}

  <!-- Text items table -->
  <section>
    {#if nrDomains.length > 0}
      <div class="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-3">
        <div class="text-sm font-medium text-amber-900">⚠ needs_review по доменам — массовое до-заполнение</div>
        <p class="mt-0.5 text-[11px] text-amber-700">
          Привяжи домен ко всем задачам прогона (каждой — её собственная ссылка), либо добавь
          его в проект (авто-резолв + будущие тексты с ним не уйдут в review).
        </p>
        <div class="mt-2 flex flex-col gap-1.5">
          {#each nrDomains as d}
            <div class="flex flex-wrap items-center gap-2 rounded border border-amber-200 bg-white px-2 py-1.5">
              <span class="font-mono text-xs text-slate-800">{d.domain}</span>
              {#if d.is_project_domain}<span class="rounded bg-emerald-100 px-1 text-[10px] text-emerald-700">в проекте</span>{/if}
              <span class="text-[11px] text-slate-500">{d.count} задач</span>
              <span class="grow"></span>
              <button type="button" onclick={() => nrBulkResolve(d.domain)} disabled={!!nrBusy}
                      class="rounded border border-amber-400 bg-white px-2.5 py-1 text-xs font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-50">
                {nrBusy === `resolve:${d.domain}` ? '…' : `Привязать ко всем (${d.count})`}
              </button>
              {#if !d.is_project_domain}
                <button type="button" onclick={() => nrAddDomain(d.domain)} disabled={!!nrBusy}
                        class="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
                  {nrBusy === `add:${d.domain}` ? '…' : '+ в проект'}
                </button>
              {/if}
            </div>
          {/each}
        </div>
      </div>
    {/if}
    {#if missingDomains.length > 0}
      <div class="mb-3 rounded-lg border border-sky-300 bg-sky-50 p-3">
        <div class="flex flex-wrap items-center gap-2">
          <button type="button" onclick={() => (showMissing = !showMissing)}
                  class="flex items-center gap-1.5 text-left text-sm font-medium text-sky-900">
            <span class="text-xs">{showMissing ? '▾' : '▸'}</span>
            Целевые домены не в проекте — {missingDomains.length}
          </button>
          <span class="grow"></span>
          <button type="button" onclick={missingAddAll} disabled={!!missingBusy}
                  class="rounded bg-sky-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-sky-700 disabled:opacity-50">
            {missingBusy === 'all' ? '…' : `Добавить все (${missingDomains.length})`}
          </button>
        </div>
        {#if showMissing}
          <p class="mt-1.5 text-[11px] text-sky-700">
            Ссылки заданы явно (CSV) → в проект автоматически не добавляются. Добавь нужные домены в проект.
          </p>
          <div class="mt-2 flex flex-col gap-1.5">
            {#each missingDomains as d}
              <div class="flex flex-wrap items-center gap-2 rounded border border-sky-200 bg-white px-2 py-1.5">
                <span class="font-mono text-xs text-slate-800">{d.domain}</span>
                <span class="text-[11px] text-slate-500">{d.count} задач</span>
                <span class="grow"></span>
                <button type="button" onclick={() => missingAddDomain(d.domain)} disabled={!!missingBusy}
                        class="rounded bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
                  {missingBusy === `add:${d.domain}` ? '…' : '+ в проект'}
                </button>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <h2 class="mr-2 text-lg font-medium text-slate-900">Texts</h2>
      {#each ['all', 'needs_review', 'pending', 'generating', 'posting', 'posted', 'failed', 'skipped'] as s}
        {@const isOn = filterStatus === s}
        {@const count = s === 'all'
          ? (progress?.total ?? null)
          : (progress?.[s as TextItemStatus] ?? null)}
        <button type="button" onclick={() => changeStatusFilter(s as TextItemStatus | 'all')}
                class="rounded-full px-3 py-1 text-xs font-medium transition"
                class:bg-brand-600={isOn}
                class:text-white={isOn}
                class:bg-slate-100={!isOn}
                class:text-slate-700={!isOn}
                class:hover:bg-slate-200={!isOn}>
          {s}{#if count !== null} <span class="ml-1 opacity-70">{count}</span>{/if}
        </button>
      {/each}
    </div>

    {#if isGenRun}
      <!-- Gen-задача: контент-фильтр (по загруженным) — текст/спины -->
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <span class="text-xs font-medium uppercase tracking-wider text-slate-400">Контент</span>
        {#each [['all', 'Все'], ['with_text', 'С текстом'], ['no_text', 'Без текста'], ['spin', 'Спины']] as [val, label]}
          {@const isOn = contentFilter === val}
          <button type="button" onclick={() => (contentFilter = val as typeof contentFilter)}
                  class="rounded-full px-3 py-1 text-xs font-medium transition"
                  class:bg-orange-500={isOn} class:text-white={isOn}
                  class:bg-orange-50={!isOn} class:text-orange-700={!isOn} class:hover:bg-orange-100={!isOn}>
            {label}
          </button>
        {/each}
      </div>
    {/if}

    {#if displayedItems.length === 0}
      <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
        Нет текстов в выбранном фильтре.
      </div>
    {:else}
      <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
        {#snippet sortHead(key: string, label: string, cls = '')}
          <th class="px-3 py-2 {cls}">
            <button type="button" onclick={() => toggleSort(key)}
                    class="inline-flex items-center gap-0.5 uppercase tracking-wider transition hover:text-slate-700"
                    class:text-brand-700={sortKey === key}>
              {label}
              <span class="text-[10px]">{sortKey === key ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}</span>
            </button>
          </th>
        {/snippet}
        <table class="min-w-full text-sm">
          <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
            <tr>
              {@render sortHead('id', 'ID')}
              {@render sortHead('link', 'Link → домен')}
              {@render sortHead('anchor', 'Anchor')}
              {#if !isLinkRun}{@render sortHead('text', 'Text')}{/if}
              {@render sortHead('status', 'Status')}
              <th class="px-3 py-2">{isLinkRun ? 'Сайт / результат' : 'Result / Error'}</th>
              {@render sortHead('posted', 'Posted')}
              <th class="px-3 py-2 text-right">Действия</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            {#each displayedItems as item (item.id)}
              <tr class="align-top">
                <td class="px-3 py-2 text-slate-500">{item.id}</td>
                <!-- Link → домен, к которому привязана задача -->
                <td class="px-3 py-2">
                  <div class="break-all font-mono text-[12px] text-slate-800">{item.link_url || '—'}</div>
                  {#if item.target_domain}
                    {#if run && projectDomains.has(normDomain(item.target_domain))}
                      <a href={`/projects/${run.project.id}/domains/${encodeURIComponent(item.target_domain)}`}
                         class="mt-0.5 block text-[11px] text-brand-600 hover:underline"
                         title="Открыть страницу домена">{item.target_domain}</a>
                    {:else}
                      <div class="mt-0.5 text-[11px] text-slate-400" title="Домена нет в проекте — добавьте, чтобы открыть его страницу">{item.target_domain}</div>
                    {/if}
                  {/if}
                </td>
                <!-- Anchor -->
                <td class="px-3 py-2 text-slate-700">{item.link_anchor || '—'}</td>
                <!-- Text (только постинг) -->
                {#if !isLinkRun}
                  <td class="px-3 py-2">
                    {#if item.text_id != null}
                      <a href={`/runs/${runId}/texts/${item.id}`} class="block hover:text-brand-600">
                        <div class="font-medium text-slate-900 hover:text-brand-600">{item.title || '— no title —'}</div>
                        <div class="mt-0.5 text-[11px] text-slate-400">{item.original_filename} · {fmtBytes(item.byte_size)}</div>
                      </a>
                    {:else}
                      <!-- gen_per_row: пустой item (спин) — текст появится после Start -->
                      <div class="text-slate-400">—</div>
                      <div class="mt-0.5 text-[11px] text-slate-400">спин · после Start</div>
                    {/if}
                  </td>
                {/if}
                <!-- Status -->
                <td class="px-3 py-2">
                  <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {itemStatusClass(item.status)}">
                    {item.status}
                  </span>
                  {#if !isLinkRun && item.status === 'posted' && item.link_verified != null}
                    {#if item.link_verified}
                      <span class="ml-1 font-semibold text-emerald-600" title="Ссылка подтверждена на странице поста">✓</span>
                    {:else}
                      <span class="ml-1 font-semibold text-red-500" title="Ссылка НЕ найдена на странице поста">✗</span>
                    {/if}
                  {/if}
                </td>
                <!-- Result / Error (для ссылок — сайт, где проставлено) -->
                <td class="px-3 py-2">
                  {#if isLinkRun}
                    {#if item.site}
                      <div class="text-slate-700">{item.site.domain}</div>
                      <div class="mt-0.5 flex items-center gap-1.5">
                        {#if item.placed_via}<span class="rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">{item.placed_via}</span>{/if}
                        {#if item.link_verified === true}
                          <span class="text-[11px] font-medium text-violet-700" title="Проверка ссылок подтвердила: ссылка есть на странице">✓ на странице</span>
                        {:else if item.link_verified === false}
                          <span class="text-[11px] font-medium text-red-500" title="Проверка ссылок: ссылки НЕТ на странице">✗ снята</span>
                        {:else if item.verified_at}
                          <span class="text-[11px] text-emerald-600" title="Подтверждена при размещении (проверь актуальность кнопкой «Проверить ссылки»)">verified ✓</span>
                        {/if}
                        {#if item.status === 'posted'}
                          <button onclick={() => removeLink(item.id)} disabled={removingItem === item.id}
                                  class="text-[11px] text-red-600 hover:underline disabled:opacity-50">
                            {removingItem === item.id ? '…' : 'снять'}
                          </button>
                        {/if}
                      </div>
                      {#if item.posted_url}<a href={item.posted_url} target="_blank" rel="noopener noreferrer" class="mt-0.5 block break-all text-[11px] text-brand-600 hover:underline">{prettyUrl(item.posted_url)}</a>{/if}
                    {:else if item.last_error}
                      <span class="text-red-600" title={item.last_error}>{item.last_error.slice(0, 120)}</span>
                    {:else}<span class="text-slate-400">—</span>{/if}
                  {:else if item.posted_url}
                    <a href={item.posted_url} target="_blank" rel="noopener noreferrer"
                       class="break-all text-brand-600 hover:underline">{prettyUrl(item.posted_url)}</a>
                    <div class="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                      {#if item.post_id}<span>post_id: {item.post_id}</span>{/if}
                      {#if item.link_verified === true}<span class="text-emerald-600" title="Ссылка подтверждена на странице">link ✓</span>
                      {:else if item.link_verified === false}<span class="text-red-500" title="Ссылка не найдена на странице">link ✗</span>{/if}
                    </div>
                  {:else if item.last_error}
                    <span class="text-red-600" title={item.last_error}>{item.last_error.slice(0, 120)}</span>
                  {:else}
                    <span class="text-slate-400">—</span>
                  {/if}
                </td>
                <!-- Posted -->
                <td class="px-3 py-2 text-xs text-slate-500">
                  {item.posted_at ? new Date(item.posted_at).toLocaleString() : '—'}
                </td>
                <!-- Действия: цветная иконка + рамочка (без заливки), тултип при наведении -->
                <td class="px-3 py-2">
                  <div class="flex items-center justify-end gap-1.5">
                    {#if item.status === 'posted'}
                      <button type="button" title="Repost — перезапостить на другой сайт (текущий исключим)"
                              onclick={() => itemAction(item.id, 'repost')} disabled={itemBusy === item.id}
                              class="inline-flex items-center justify-center rounded-md border border-emerald-300 p-1.5 text-emerald-600 transition hover:bg-emerald-50 disabled:opacity-40">
                        <RotateCw size={15} />
                      </button>
                    {:else if item.status === 'posting' || item.status === 'generating'}
                      <span class="inline-flex h-7 w-7 items-center justify-center text-slate-400">
                        <RefreshCw size={14} class="animate-spin" />
                      </span>
                    {:else}
                      {#if isGenRun && item.text_id == null}
                        <button type="button" title="Сгенерировать текст"
                                onclick={() => itemAction(item.id, 'generate')} disabled={itemBusy === item.id}
                                class="inline-flex items-center justify-center rounded-md border border-orange-300 p-1.5 text-orange-600 transition hover:bg-orange-50 disabled:opacity-40">
                          <Wand2 size={15} />
                        </button>
                      {:else}
                        {#if isGenRun}
                          <button type="button" title="Перегенерировать текст"
                                  onclick={() => itemAction(item.id, 'regenerate')} disabled={itemBusy === item.id}
                                  class="inline-flex items-center justify-center rounded-md border border-orange-300 p-1.5 text-orange-600 transition hover:bg-orange-50 disabled:opacity-40">
                            <RefreshCw size={15} />
                          </button>
                        {/if}
                        {#if isLinkRun || item.text_id != null}
                          <button type="button" title="Запостить этот айтем"
                                  onclick={() => itemAction(item.id, 'post')} disabled={itemBusy === item.id}
                                  class="inline-flex items-center justify-center rounded-md border border-emerald-300 p-1.5 text-emerald-600 transition hover:bg-emerald-50 disabled:opacity-40">
                            <Send size={15} />
                          </button>
                        {/if}
                      {/if}
                    {/if}
                    {#if item.status !== 'posting' && item.status !== 'generating'}
                      <button type="button" title="Удалить айтем из задачи (безвозвратно)"
                              onclick={() => deleteItem(item)} disabled={itemBusy === item.id}
                              class="inline-flex items-center justify-center rounded-md border border-red-300 p-1.5 text-red-500 transition hover:bg-red-50 disabled:opacity-40">
                        <Trash2 size={15} />
                      </button>
                    {/if}
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    {#if hasMore}
      <div class="mt-3 flex justify-center">
        <button type="button" onclick={loadMore} disabled={loadingMore}
                class="rounded-md border border-slate-300 px-4 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50">
          {loadingMore ? 'Загрузка…' : `Показать ещё (+${PER_PAGE})`}
        </button>
      </div>
    {/if}
  </section>

  {#if run && isActiveStatus(run.status)}
    <p class="text-xs text-slate-400">
      {sseConnected ? '🟢 Live (SSE)' : '⚪ Polling…'} — прогресс обновляется в реальном времени.
    </p>
  {/if}
</div>

<!-- Edit run params modal -->
{#if editOpen && run}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (editOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-lg overflow-auto rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Изменить параметры · run #{run.id}</h2>
      <p class="mt-1 text-xs text-slate-500">Статус: {run.status}. Меняются только параметры постинга — контент и тип задачи фиксированы.</p>

      <!-- Read-only контекст: что нельзя менять -->
      <div class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
        <div><span class="text-slate-400">Проект:</span> <span class="text-slate-700">{run.project.name}</span></div>
        <div><span class="text-slate-400">Тип:</span> <span class="text-slate-700">{run.task_type === 'post' ? 'Пост' : run.task_type === 'homepage_link' ? 'С главной' : 'Сквозная'}</span></div>
        <div><span class="text-slate-400">Источник:</span> <span class="text-slate-700">{run.content_source}</span></div>
        <div><span class="text-slate-400">Текстов:</span> <span class="text-slate-700">{run.total_texts}</span></div>
        <div class="col-span-2 truncate"><span class="text-slate-400">Имя:</span> <span class="text-slate-700">{run.name}</span></div>
      </div>

      <div class="mt-4 space-y-2.5">
        <!-- 1. Пул сайтов и доступов -->
        <div class="rounded-md border border-slate-200">
          <button type="button" onclick={() => (secPoolOpen = !secPoolOpen)}
                  class="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-slate-50">
            <span class="flex items-center gap-1.5 text-sm font-medium text-slate-700">
              <span class="text-[10px] text-slate-400">{secPoolOpen ? '▼' : '▶'}</span> Пул сайтов и доступов
            </span>
            <span class="text-[11px] text-slate-400">{ePoolSummary}</span>
          </button>
          {#if secPoolOpen}
            <div class="border-t border-slate-100 px-3 py-3">
              <div class="grid grid-cols-2 gap-2">
                <input bind:value={eSiteLangs} placeholder="язык: de,en" aria-label="Языки сайтов"
                       class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
                <input bind:value={eSiteTlds} placeholder="tld: de,at,ch" aria-label="TLD доменов"
                       class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
              </div>
              <p class="mt-1 text-[11px] text-slate-400">Только сайты с этим <b>языком</b> и <b>TLD</b> (через запятую). Пусто = все.</p>
              <label class="mt-2 flex cursor-pointer items-start gap-2 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-2">
                <input type="checkbox" bind:checked={ePoolFallback} class="mt-0.5" />
                <span class="text-[11px] text-slate-600">
                  <b>Авто-добор по всему пулу</b> — если доступы под фильтром (язык/TLD/теги) кончились,
                  не вставать в <code>need_more_admins</code>, а продолжить по остальному разрешённому пулу.
                  Сначала точно проставит по фильтру, потом доберёт остальным.
                </span>
              </label>
              <div class="mt-2 flex flex-wrap items-center gap-1.5">
                <button type="button" onclick={() => (ePoolMode = ePoolMode === 'tags' ? 'all' : 'tags')}
                        class="rounded-full border px-3 py-1 text-xs font-medium {ePoolMode === 'tags' ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">По тегам</button>
                <button type="button" onclick={() => (ePoolMode = ePoolMode === 'domains' ? 'all' : 'domains')}
                        class="rounded-full border px-3 py-1 text-xs font-medium {ePoolMode === 'domains' ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">Свой список доменов</button>
                {#if ePoolMode === 'all'}<span class="text-[11px] text-slate-400">сейчас: весь пул</span>{/if}
              </div>
              {#if ePoolMode === 'tags'}
                {#if eAvailableTags.length === 0}
                  <p class="mt-2 text-[11px] text-slate-400">Тегов пока нет.</p>
                {:else}
                  {#if eSiteTags.length}
                    <div class="mt-2 flex flex-wrap items-center gap-1.5">
                      {#each eSiteTags as tag}
                        <button type="button" onclick={() => eToggleTag(tag)}
                                class="flex items-center gap-1 rounded-full border border-brand-400 bg-brand-50 px-2.5 py-1 text-[12px] text-brand-700">
                          {tag} <X size={11} class="inline-block" />
                        </button>
                      {/each}
                      <button type="button" onclick={() => (eSiteTags = [])} class="px-1 text-[11px] text-slate-400 hover:text-slate-600">сбросить</button>
                    </div>
                  {/if}
                  <input bind:value={eTagSearch} placeholder={`поиск среди ${eAvailableTags.length} тегов…`}
                         class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                  <div class="mt-2 flex max-h-32 flex-wrap gap-1.5 overflow-auto">
                    {#each eTagResults as tag}
                      <button type="button" onclick={() => eToggleTag(tag)}
                              class="rounded-full border border-slate-300 bg-white px-2.5 py-1 text-[12px] text-slate-600 hover:bg-slate-50">+ {tag}</button>
                    {/each}
                    {#if eTagResults.length === 0}
                      <p class="text-[11px] text-slate-400">{eTagSearch.trim() ? 'Ничего не найдено.' : 'Все теги выбраны.'}</p>
                    {/if}
                  </div>
                  {#if eTagResultsMore > 0}<p class="mt-1 text-[11px] text-slate-400">…ещё {eTagResultsMore} — уточни поиск.</p>{/if}
                  <p class="mt-1 text-[11px] text-slate-400">Выбрано: <b>{eSiteTags.length}</b> · берём сайты с доступом из батча с одним из выбранных тегов.</p>
                {/if}
              {:else if ePoolMode === 'domains'}
                {#if (run.site_domains_count || run.site_domains_file) && !eSiteDomains.trim()}
                  <p class="mt-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-1.5 text-[11px] text-slate-500">Текущий список: <b>{run.site_domains_count ?? '—'}</b> дом.{run.site_domains_file ? ' (файл)' : ''} — введи новый, чтобы заменить.</p>
                {/if}
                <textarea bind:value={eSiteDomains} rows="4"
                          placeholder="по домену в строке (или через запятую)"
                          class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono"></textarea>
                <p class="mt-1 text-[11px] text-slate-400">Постим только на эти домены — креды к ним берём из базы.</p>
              {/if}
            </div>
          {/if}
        </div>

        <!-- 2. Расписание и темп -->
        <div class="rounded-md border border-slate-200">
          <button type="button" onclick={() => (secSchedOpen = !secSchedOpen)}
                  class="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-slate-50">
            <span class="flex items-center gap-1.5 text-sm font-medium text-slate-700">
              <span class="text-[10px] text-slate-400">{secSchedOpen ? '▼' : '▶'}</span> Расписание и темп
            </span>
            <span class="text-[11px] text-slate-400">{eSchedSummary}</span>
          </button>
          {#if secSchedOpen}
            <div class="space-y-3 border-t border-slate-100 px-3 py-3">
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label for="ed_sched" class="block text-sm font-medium text-slate-700">Scheduled start <span class="text-slate-400">(пусто = сразу)</span></label>
                  <input id="ed_sched" type="datetime-local" bind:value={eSchedFor}
                         class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                </div>
                <div>
                  <label for="ed_spread" class="block text-sm font-medium text-slate-700">Разбить на дней</label>
                  <input id="ed_spread" type="number" min="0" max="365" bind:value={eSpread}
                         class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                </div>
              </div>
              <div>
                <span class="block text-sm font-medium text-slate-700">Окно публикации <span class="text-slate-400">(пусто = стандартное)</span></span>
                <div class="mt-1 grid grid-cols-2 gap-2">
                  <input type="date" bind:value={ePubFrom} max={ePubTo || editToday} aria-label="Publish from"
                         class="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                  <input type="date" bind:value={ePubTo} min={ePubFrom || undefined} max={editToday} aria-label="Publish to"
                         class="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                </div>
                {#if eWindowInvalid}
                  <p class="mt-1 text-[11px] text-red-600">Заполни обе даты, From не позже To.</p>
                {:else if eWindowFuture}
                  <p class="mt-1 text-[11px] text-amber-600">Дата позже сегодня — посты уйдут в Scheduled. Выбери не позже сегодняшней.</p>
                {/if}
              </div>
            </div>
          {/if}
        </div>

        <!-- 3. Параметры постинга -->
        <div class="rounded-md border border-slate-200">
          <button type="button" onclick={() => (secPostOpen = !secPostOpen)}
                  class="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-slate-50">
            <span class="flex items-center gap-1.5 text-sm font-medium text-slate-700">
              <span class="text-[10px] text-slate-400">{secPostOpen ? '▼' : '▶'}</span> Параметры постинга
            </span>
            <span class="text-[11px] text-slate-400">{ePostSummary}</span>
          </button>
          {#if secPostOpen}
            <div class="space-y-3 border-t border-slate-100 px-3 py-3">
              <div>
                <span class="block text-sm font-medium text-slate-700">Priority</span>
                <div class="mt-1 flex gap-1">
                  {#each [['low', 'Low'], ['normal', 'Normal'], ['high', 'High']] as [val, label]}
                    {@const on = ePriority === val}
                    <button type="button" onclick={() => (ePriority = val as 'low' | 'normal' | 'high')}
                            class="flex-1 rounded-md border px-2 py-1.5 text-xs font-medium {on ? 'border-brand-600 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">{label}</button>
                  {/each}
                </div>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label for="ed_mpps" class="block text-sm font-medium text-slate-700">Max posts / site</label>
                  <input id="ed_mpps" type="number" min="1" max="1000" bind:value={eMaxPosts}
                         class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                  <p class="mt-1 text-[11px] text-slate-400"><b>1</b> = «1 сайт = 1 пост». Подними, чтобы добрать из использованных.</p>
                </div>
                <div>
                  <label for="ed_proxy" class="block text-sm font-medium text-slate-700">Proxy pool</label>
                  <select id="ed_proxy" bind:value={eProxySelector}
                          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    {#if ePoolStats.all_active > 0}
                      <option value="all">All proxies ({ePoolStats.all_active})</option>
                      {#each Object.entries(ePoolStats.providers) as [name, cnt]}
                        {#if cnt > 0}<option value={`provider:${name}`}>Provider: {name} ({cnt})</option>{/if}
                      {/each}
                    {/if}
                    {#if eProxySelector && eProxySelector !== 'direct' && eProxySelector !== 'all' && !eProxySelector.startsWith('provider:')}
                      <option value={eProxySelector}>{eProxySelector}</option>
                    {/if}
                    <option value="direct">— без прокси (direct) —</option>
                  </select>
                </div>
              </div>
              {#if !isLinkRun}
                <div>
                  <label for="ed_method" class="block text-sm font-medium text-slate-700">Метод постинга</label>
                  <select id="ed_method" bind:value={eMethod} class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    <option value="auto">Auto — XML-RPC → wp-admin</option>
                    <option value="xmlrpc_only">XML-RPC only</option>
                    <option value="admin_only">wp-admin only</option>
                  </select>
                </div>
                <div>
                  <label for="ed_verify" class="block text-sm font-medium text-slate-700">Валидация ссылки</label>
                  <select id="ed_verify" bind:value={eVerify} class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    <option value="mark">Отметка ✓/✗</option>
                    <option value="auto">Автовалидация (перепост)</option>
                  </select>
                </div>
              {/if}
            </div>
          {/if}
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (editOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="button" onclick={saveEdit} disabled={editBusy || eWindowInvalid || eWindowFuture}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {editBusy ? 'Сохраняю…' : 'Сохранить'}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

