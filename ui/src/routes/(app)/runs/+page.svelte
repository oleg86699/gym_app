<script lang="ts">
  import { AlertTriangle, ArrowRight, Check, HelpCircle, Play, X } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onDestroy, onMount } from 'svelte'

  import {
    aiSettings,
    postings as postingsApi,
    projects as projectsApi,
    proxies as proxiesApi,
    users as usersApi,
    wpSites as wpSitesApi,
  } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { runModeLabel } from '$lib/runLabels'
  import type {
    AiModel,
    AiProvider,
    PostingRun,
    PostingRunPriority,
    PostingRunStatus,
    Project,
    PromptTemplate,
    Proxy,
    QueueItem,
    QueueLinkCheckItem,
    User,
  } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let items = $state<PostingRun[]>([])
  let projectsForFilter = $state<Project[]>([])
  let usersForFilter = $state<User[]>([])
  let initialLoading = $state(true)
  // Two-level delete: только super_admin может смотреть удалённые + restore/purge
  let isSuper = $derived($currentUser?.is_super_admin ?? false)
  let showDeleted = $state(false)

  // ─── Global queue — общая видимая очередь по всем юзерам ─────────
  let queue = $state<QueueItem[]>([])
  let linkChecks = $state<QueueLinkCheckItem[]>([])
  async function refreshQueue() {
    try {
      const res = await postingsApi.queue()
      queue = res.items
      linkChecks = res.link_checks ?? []
    } catch {
      queue = []
      linkChecks = []
    }
  }

  // ─── Filters ─────────────────────────────────────────────────────
  // Default = all (никаких фильтров)
  let search = $state('')
  let filterStatus = $state<'all' | 'active' | 'scheduled' | 'done' | 'failed'>('all')
  let filterProjectId = $state<number | null>(null)
  let filterCreatorId = $state<number | null>(null)

  const STATUS_PRESETS: { value: typeof filterStatus; label: string; statuses: string[] }[] = [
    { value: 'all', label: 'All', statuses: [] },
    { value: 'active', label: 'Active', statuses: ['unpacking', 'queued', 'running', 'paused'] },
    { value: 'scheduled', label: 'Scheduled', statuses: ['scheduled'] },
    { value: 'done', label: 'Done', statuses: ['done'] },
    { value: 'failed', label: 'Failed/Cancelled', statuses: ['failed', 'cancelled', 'interrupted', 'need_more_admins'] },
  ]

  let anyFilterActive = $derived(
    search !== '' ||
      filterStatus !== 'all' ||
      filterProjectId !== null ||
      filterCreatorId !== null,
  )

  // Debounced refresh: чтобы search не дёргал API на каждой клавише
  let searchDebounce: ReturnType<typeof setTimeout> | null = null
  function onSearchInput() {
    if (searchDebounce) clearTimeout(searchDebounce)
    searchDebounce = setTimeout(() => refresh(false), 300)
  }

  function clearFilters() {
    search = ''
    filterStatus = 'all'
    filterProjectId = null
    filterCreatorId = null
    refresh(false)
  }

  let pollTimer: ReturnType<typeof setInterval> | null = null

  function statusesForCurrentPreset(): string[] {
    return STATUS_PRESETS.find((p) => p.value === filterStatus)?.statuses ?? []
  }

  async function refresh(showSpinner = false) {
    if (showSpinner) initialLoading = true
    try {
      const res = await postingsApi.list({
        limit: 200,
        statuses: statusesForCurrentPreset(),
        project_id: filterProjectId ?? undefined,
        created_by: filterCreatorId ?? undefined,
        search: search || undefined,
        include_deleted: (isSuper && showDeleted) || undefined,
      })
      items = res.items
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      if (showSpinner) initialLoading = false
    }
  }

  async function loadProjectsForFilter() {
    try {
      const r = await projectsApi.list({ limit: 200 })
      projectsForFilter = r.items
    } catch {
      projectsForFilter = []
    }
  }

  async function loadUsersForFilter() {
    // Чтение списка юзеров — не у всех ролей. Не падаем при 403.
    try {
      const r = await usersApi.list({ limit: 200 })
      usersForFilter = r.items
    } catch {
      usersForFilter = []
    }
  }

  async function startRun(r: PostingRun) {
    if (!confirm(`Запустить run «${r.name}» (${r.total_texts} текстов)?\n\nПостинг начнётся немедленно.`)) return
    try {
      await postingsApi.start(r.id)
      showToast('success', `Run #${r.id} запущен`)
      await refresh(false)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function restartRun(r: PostingRun) {
    const remaining = r.total_texts - r.posted_count
    if (!confirm(
      `Перезапустить run «${r.name}»?\n\n` +
      `${r.posted_count} posted останутся, ${remaining} оставшихся будут сброшены в pending.`,
    )) return
    try {
      const res = await postingsApi.restart(r.id)
      showToast('success', `Restarted: ${res.items_reset} items reset`)
      await refresh(false)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // Two-level delete (super_admin): вернуть soft-deleted / удалить полностью
  async function restoreRun(r: PostingRun) {
    try {
      await postingsApi.restore(r.id)
      showToast('success', `Run «${r.name}» восстановлен`)
      await refresh(false)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
  async function purgeRun(r: PostingRun) {
    if (!confirm(
      `ПОЛНОСТЬЮ удалить run «${r.name}» из БД?\n\n` +
      `Это необратимо — сотрёт все text_items и результаты постинга. ` +
      `Отмены не будет.`,
    )) return
    try {
      await postingsApi.purge(r.id)
      showToast('success', `Run «${r.name}» удалён полностью`)
      await refresh(false)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function hasActiveRuns(): boolean {
    const active = new Set(['unpacking', 'queued', 'running', 'paused'])
    return items.some((r) => active.has(r.status))
  }

  function tickPoll() {
    // Очередь обновляем всегда — она может пополняться задачами других юзеров
    refreshQueue()
    if (hasActiveRuns()) refresh(false)
  }

  async function loadAvailableProxies() {
    try {
      const r = await proxiesApi.list({ status: 'active', limit: 500 })
      availableProxies = r.items
    } catch {
      availableProxies = []
    }
  }

  // Proxy pool stats для New run dropdown — какие пулы доступны и сколько в каждом
  let poolStats = $state<{ all_active: number; providers: Record<string, number> }>({ all_active: 0, providers: {} })
  async function loadPoolStats() {
    try {
      poolStats = await proxiesApi.pools()
    } catch {
      poolStats = { all_active: 0, providers: {} }
    }
  }

  async function loadCredentialTags() {
    try {
      availableTags = await wpSitesApi.credentialTags()
    } catch {
      availableTags = []
    }
  }

  onMount(async () => {
    await Promise.all([
      refresh(true),
      refreshQueue(),
      loadProjectsForFilter(),
      loadUsersForFilter(),
      loadAvailableProxies(),
      loadPoolStats(),
      loadCredentialTags(),
    ])
    // Пришли со страницы проекта (/runs?new=<projectId>) — открываем единую
    // форму создания run'а с предвыбранным проектом.
    const newParam = page.url.searchParams.get('new')
    if (newParam) {
      openCreate()
      const pid = Number(newParam)
      if (!Number.isNaN(pid)) newProjectId = pid
    }
    pollTimer = setInterval(tickPoll, 5000)
  })
  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer)
    if (searchDebounce) clearTimeout(searchDebounce)
  })

  function statusBadgeClass(s: PostingRunStatus): string {
    switch (s) {
      case 'unpacking':
      case 'scheduled':
      case 'queued':
        return 'bg-amber-100 text-amber-700'
      case 'ready':
        return 'bg-indigo-100 text-indigo-700'
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

  function progressPct(r: PostingRun): number {
    if (r.total_texts === 0) return 0
    // cap 100% — на in-flight run-ах счётчики бампятся per-попытку и при ретраях
    // могут кратко превышать total (бэкенд приводит к факту при финализации).
    return Math.min(100, Math.round(((r.posted_count + r.failed_count + r.skipped_count) / r.total_texts) * 100))
  }

  let modesOpen = $state(false)  // справка «режимы работы + форматы файлов»
  let modesTab = $state<'post' | 'sitewide' | 'homepage' | 'common'>('post')

  // ─── Create run modal ──────────────────────────────────────────────

  let createOpen = $state(false)
  let createBusy = $state(false)
  let newTaskType = $state<'post' | 'sitewide_link' | 'homepage_link'>('post')
  // Методы скрытия ссылки/текста (link-режимы): контент оборачивается в скрывающий
  // <div>, ссылка остаётся в исходнике (verify проходит). Несколько → случайный на сайт.
  const HIDE_METHODS: { key: string; label: string; hint: string }[] = [
    { key: 'clip', label: '1px (sr-only)', hint: 'position:absolute + clip 1px' },
    { key: 'display_none', label: 'display:none', hint: 'убран из разметки' },
    { key: 'visibility', label: 'visibility:hidden', hint: 'невидим, место держит' },
    { key: 'opacity', label: 'opacity:0', hint: 'прозрачен, кликабелен' },
    { key: 'hidden_attr', label: 'hidden (атрибут)', hint: '<div hidden>' },
    { key: 'offscreen', label: 'за экран', hint: 'left:-99999px' },
  ]
  let hideMethods = $state<string[]>([])  // пусто = без скрытия
  function toggleHide(k: string) {
    hideMethods = hideMethods.includes(k) ? hideMethods.filter((x) => x !== k) : [...hideMethods, k]
  }
  // «Пост»: источник текстов — архив .txt / готовые тексты CSV / генерация-reuse
  let postSource = $state<'zip' | 'csv_direct' | 'gen'>('zip')
  // csv_direct: инжектить ли ссылку из строки в тело (по умолчанию НЕТ)
  let csvInjectLink = $state(false)
  // Content Engine csv_campaign (C2): режим контента + AI
  let campContentMode = $state<'gen_per_post' | 'gen_per_row' | 'reuse'>('gen_per_post')
  let campRunMode = $state<'auto' | 'manual'>('manual')
  let campLang = $state('English')
  let campPromptId = $state<number | null>(null)
  let campModelId = $state<number | null>(null)
  let campProviders = $state<AiProvider[]>([])  // доступные ключи (с ≥1 content-моделью)
  let campProviderId = $state<number | null>(null)  // выбранный ключ
  let campProviderLabel = $state<Record<number, string>>({})
  let campPrompts = $state<PromptTemplate[]>([])
  let campAiLoaded = $state(false)
  const isContentModel = (m: AiModel) => m.is_active && (m.purpose === 'content' || m.purpose === 'any')
  // модели выбранного ключа
  let campKeyModels = $derived(
    (campProviders.find((p) => p.id === campProviderId)?.models ?? []).filter(isContentModel),
  )
  function firstModelOfKey(pid: number | null): number | null {
    const m = campProviders.find((p) => p.id === pid)?.models.find(isContentModel)
    return m?.id ?? null
  }
  function onCampKeyChange() {
    campModelId = firstModelOfKey(campProviderId)  // сменили ключ → первая его модель
  }
  let newProjectId = $state<number | null>(null)
  let newName = $state('')
  let nameTouched = $state(false)  // юзер правил имя руками → не перетираем из файла
  let newFile = $state<File | null>(null)
  let newPriority = $state<PostingRunPriority>('normal')
  let newScheduledFor = $state('')
  let newSpreadDays = $state(0)  // drip-feed: размазать постинг на N дней (0 = сразу)
  let newMaxPostsPerSite = $state(1)  // сколько раз один сайт можно юзать в задаче (1 = «1 сайт = 1 пост»)
  // Аккордеон «Дополнительно» — 3 смысловые категории (несколько открытых сразу)
  let secPoolOpen = $state(false)
  let secSchedOpen = $state(false)
  let secPostOpen = $state(false)
  let newSiteLangs = $state('')     // фильтр пула: языки сайтов через запятую
  let newSiteTlds = $state('')      // фильтр пула: TLD через запятую
  let newPoolFallback = $state(false)  // добить по всему пулу при исчерпании фильтра
  // Источник пула доступов: all = весь пул; tags = по тегам кредов; domains = свой список
  let poolMode = $state<'all' | 'tags' | 'domains'>('all')
  let newSiteTags = $state<string[]>([])   // выбранные теги (для poolMode='tags')
  let newSiteDomains = $state('')           // свой список доменов inline (для poolMode='domains')
  let newSiteDomainsKey = $state<string | null>(null)  // большой список файлом → MinIO-ключ
  let domainsFileCount = $state(0)          // сколько доменов в загруженном файле
  let domainsUploading = $state(false)
  let availableTags = $state<string[]>([])  // теги батчей (из credential-tags)
  let tagSearch = $state('')                // поиск по тегам (для 100+)
  const TAG_RESULTS_CAP = 24                // не вываливаем 100+ тегов сразу
  let domainCount = $derived(
    newSiteDomainsKey
      ? domainsFileCount
      : newSiteDomains.split(/[\n,\s]+/).map((d) => d.trim()).filter(Boolean).length,
  )
  let filteredTags = $derived.by(() => {
    const q = tagSearch.trim().toLowerCase()
    return q ? availableTags.filter((t) => t.toLowerCase().includes(q)) : availableTags
  })
  // Результаты поиска тегов = отфильтрованные минус уже выбранные, с потолком
  let tagResultsAll = $derived(filteredTags.filter((t) => !newSiteTags.includes(t)))
  let tagResults = $derived(tagResultsAll.slice(0, TAG_RESULTS_CAP))
  let tagResultsMore = $derived(Math.max(0, tagResultsAll.length - TAG_RESULTS_CAP))
  function toggleTag(tag: string) {
    newSiteTags = newSiteTags.includes(tag)
      ? newSiteTags.filter((t) => t !== tag)
      : [...newSiteTags, tag]
  }
  async function uploadDomainsFile(file: File | null) {
    if (!file) return
    domainsUploading = true
    try {
      const r = await postingsApi.uploadDomainList(file)
      newSiteDomainsKey = r.key
      domainsFileCount = r.count
      newSiteDomains = ''  // файл имеет приоритет над textarea
      showToast('success', `Загружено доменов: ${r.count}`)
    } catch (e) {
      newSiteDomainsKey = null
      domainsFileCount = 0
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { domainsUploading = false }
  }
  function clearDomainsFile() {
    newSiteDomainsKey = null
    domainsFileCount = 0
  }
  // Сводки для свёрнутых категорий — видно настройки не разворачивая
  let poolSummary = $derived(
    poolMode === 'tags' ? `по тегам: ${newSiteTags.length || '—'}`
    : poolMode === 'domains' ? `домены: ${domainCount || '—'}`
    : 'весь пул',
  )
  let schedSummary = $derived([
    newScheduledFor ? 'по расписанию' : 'сразу',
    (newPublishFrom && newPublishTo) ? 'своё окно' : 'станд. окно',
    newSpreadDays > 0 ? `drip ${newSpreadDays}д` : 'без drip',
  ].join(' · '))
  let postSummary = $derived([
    newPriority,
    `${newMaxPostsPerSite || 1}/сайт`,
    newProxySelector === 'direct' ? 'без прокси' : newProxySelector === 'all' ? 'все прокси' : newProxySelector,
    ...(newTaskType === 'post' ? [newPostingMethod] : []),
  ].join(' · '))
  // Селектор прокси-пула:
  //   "direct" — без прокси
  //   "all" — все active
  //   "provider:webshare" — все active от провайдера
  //   "single:<id>" — один конкретный proxy
  let newProxySelector = $state<string>('direct')
  let newPostingMethod = $state<'auto' | 'xmlrpc_only' | 'admin_only'>('auto')
  let newPostVerify = $state<'mark' | 'auto'>('mark')
  // Окно публикации этого прогона (пусто = глобальный дефолт из settings)
  let newPublishFrom = $state('')
  let newPublishTo = $state('')
  const today = new Date().toISOString().slice(0, 10)
  // Окно: либо обе даты, либо обе пустые, и From <= To.
  let newWindowInvalid = $derived.by(() => {
    const a = newPublishFrom, b = newPublishTo
    if (!a && !b) return false
    if (!a || !b) return true
    return a > b
  })
  // Дата в будущем → WP спрячет пост в Scheduled.
  let newWindowFuture = $derived(
    (!!newPublishFrom && newPublishFrom > today) || (!!newPublishTo && newPublishTo > today),
  )

  // Дефолт = «all proxies» если они есть, иначе direct
  function pickDefaultPoolSelector(): string {
    return poolStats.all_active > 0 ? 'all' : 'direct'
  }
  let availableProxies = $state<Proxy[]>([])

  let manageableProjects = $derived.by(() => {
    const u = $currentUser
    if (!u) return []
    if (u.is_super_admin) return projectsForFilter
    const isGroupAdmin = u.roles.some((r) => r.name === 'group_admin')
    return projectsForFilter.filter((p) => {
      if (p.owner.id === u.id) return true
      if (isGroupAdmin && p.owner_group?.id === u.group?.id) return true
      return false
    })
  })

  let canCreate = $derived(manageableProjects.length > 0)

  function nowStamp(): string {
    return new Date().toISOString().slice(0, 16).replace('T', ' ')
  }

  // выбор файла: подставляем имя рана из имени файла + дата (если не правили руками)
  function applyFile(f: File | null) {
    newFile = f
    if (f && !nameTouched) {
      const base = f.name.replace(/\.[^.]+$/, '').slice(0, 200)
      newName = `${base} ${nowStamp()}`
    }
  }

  function openCreate() {
    newTaskType = 'post'
    postSource = 'zip'
    csvInjectLink = false
    newProjectId = filterProjectId ?? manageableProjects[0]?.id ?? null
    nameTouched = false
    newName = `Run ${nowStamp()}`
    newFile = null
    newPriority = 'normal'
    newScheduledFor = ''
    newPublishFrom = ''
    newPublishTo = ''
    newProxySelector = pickDefaultPoolSelector()
    newPostingMethod = 'auto'
    newPostVerify = 'mark'
    linkCandidates = null
    campContentMode = 'gen_per_post'
    campRunMode = 'manual'
    campLang = 'English'
    newSpreadDays = 0
    newMaxPostsPerSite = 1
    secPoolOpen = false
    secSchedOpen = false
    secPostOpen = false
    newSiteLangs = ''
    newSiteTlds = ''
    newPoolFallback = false
    poolMode = 'all'
    newSiteTags = []
    newSiteDomains = ''
    newSiteDomainsKey = null
    domainsFileCount = 0
    tagSearch = ''
    createOpen = true
  }

  async function refreshLinkCandidates() {
    if (newTaskType === 'post' || !newProjectId) { linkCandidates = null; return }
    try {
      linkCandidates = (await postingsApi.linkCandidates(newProjectId)).candidates
    } catch { linkCandidates = null }
  }

  async function loadCampaignAi() {
    if (campAiLoaded) return
    try {
      const [provs, prompts] = await Promise.all([aiSettings.listProviders(), aiSettings.listPrompts()])
      const meId = $currentUser?.id ?? null
      // ключи (провайдеры) с ≥1 активной content-моделью
      campProviders = provs.filter((p) => p.is_active && p.models.some(isContentModel))
      const who = (p: AiProvider) =>
        p.owner_user_id && p.owner_user_id === meId ? 'мой' : p.shared_all ? 'общий' : 'команда'
      campProviderLabel = Object.fromEntries(
        campProviders.map((p): [number, string] => [p.id, `${p.name} (${p.type}) — ${who(p)}`]),
      )
      campPrompts = prompts
      // по умолчанию — свой ключ, иначе первый; модель — первая content-модель этого ключа
      const own = campProviders.find((p) => p.owner_user_id === meId)
      campProviderId = (own ?? campProviders[0])?.id ?? null
      campModelId = firstModelOfKey(campProviderId)
      campPromptId = campPrompts[0]?.id ?? null
      campAiLoaded = true
    } catch { /* нет ключей — режим reuse всё равно работает */ }
  }

  async function selectTaskType(t: 'post' | 'sitewide_link' | 'homepage_link') {
    newTaskType = t
    if (t === 'post') { linkCandidates = null; return }
    await refreshLinkCandidates()
  }

  async function selectPostSource(s: 'zip' | 'csv_direct' | 'gen') {
    postSource = s
    newFile = null
    if (s === 'gen') await loadCampaignAi()
  }

  let linkCandidates = $state<number | null>(null)

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    if (!newProjectId) { showToast('error', 'Select a project'); return }
    if (!newFile) { showToast('error', 'Загрузи файл'); return }
    if (newWindowInvalid) { showToast('error', 'Окно публикации: заполни обе даты, From не позже To'); return }
    if (newWindowFuture) { showToast('error', 'Окно публикации: дата позже сегодня — выбери не позже сегодняшней'); return }
    createBusy = true
    try {
      const base = {
        name: newName, priority: newPriority,
        max_posts_per_site: newMaxPostsPerSite || 1,
        scheduled_for: newScheduledFor ? new Date(newScheduledFor).toISOString() : null,
        publish_from: newPublishFrom || null, publish_to: newPublishTo || null,
        spread_days: newSpreadDays || 0,
        proxy_selector: newProxySelector, posting_method: newPostingMethod,
        post_verify: newPostVerify,
        pool_fallback: newPoolFallback,
        site_langs: newSiteLangs.trim() || null, site_tlds: newSiteTlds.trim() || null,
        site_tags: poolMode === 'tags' ? (newSiteTags.join(',') || null) : null,
        site_domains: poolMode === 'domains' && !newSiteDomainsKey ? (newSiteDomains.trim() || null) : null,
        site_domains_key: poolMode === 'domains' ? newSiteDomainsKey : null,
      }
      if (newTaskType === 'post' && postSource === 'zip') {
        const run = await postingsApi.create(newProjectId, newFile, base)
        showToast('success', `Run "${run.name}" создан`)
      } else if (newTaskType === 'post' && postSource === 'csv_direct') {
        const run = await postingsApi.createCsvDirect(newProjectId, newFile, { ...base, csv_inject_link: csvInjectLink })
        showToast('success', `Run "${run.name}" создан (готовые тексты)`)
      } else if (newTaskType === 'post' && postSource === 'gen') {
        if (campContentMode !== 'reuse' && !campModelId) {
          showToast('error', 'Для генерации нужна AI-модель (AI Settings)'); createBusy = false; return
        }
        const run = await postingsApi.createCampaign(newProjectId, newFile, {
          ...base, content_mode: campContentMode, run_mode: campRunMode,
          prompt_template_id: campContentMode === 'reuse' ? null : campPromptId,
          ai_model_id: campContentMode === 'reuse' ? null : campModelId,
          language: campLang.trim() || null,
        })
        showToast('success', campRunMode === 'manual'
          ? `Кампания "${run.name}" создана. Идёт генерация → ревью → Start.`
          : `Кампания "${run.name}" создана, генерация запущена.`)
      } else {
        // link-режимы: файл anchor,link,count (count = на сколько сайтов)
        const run = await postingsApi.createLinkRun(newProjectId, newFile, {
          name: newName, task_type: newTaskType as 'sitewide_link' | 'homepage_link',
          priority: newPriority,
          max_posts_per_site: newMaxPostsPerSite || 1,
          scheduled_for: newScheduledFor ? new Date(newScheduledFor).toISOString() : null,
          publish_from: newPublishFrom || null, publish_to: newPublishTo || null,
          spread_days: newSpreadDays || 0,
          proxy_selector: newProxySelector,
          pool_fallback: newPoolFallback,
        site_langs: newSiteLangs.trim() || null, site_tlds: newSiteTlds.trim() || null,
          site_tags: poolMode === 'tags' ? (newSiteTags.join(',') || null) : null,
          site_domains: poolMode === 'domains' && !newSiteDomainsKey ? (newSiteDomains.trim() || null) : null,
          site_domains_key: poolMode === 'domains' ? newSiteDomainsKey : null,
          hide_methods: hideMethods,
        })
        showToast('success', `Link-run "${run.name}" создан (${run.total_texts} целей). Запусти кнопкой Start.`)
      }
      createOpen = false
      await refresh(false)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      createBusy = false
    }
  }
</script>

<div class="space-y-4">
  <div class="flex items-start justify-between gap-4">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Runs</h1>
      <p class="mt-1 text-sm text-slate-500">
        Все прогоны постинга по всем проектам, к которым у тебя есть доступ.
      </p>
    </div>
    <div class="flex shrink-0 items-center gap-2">
      <button type="button" onclick={() => (modesOpen = true)}
              title="Инструкция: как работает каждый режим + форматы файлов"
              aria-label="Инструкция по режимам и форматам"
              class="inline-flex items-center justify-center rounded-md border border-slate-300 p-1.5 text-slate-600 hover:bg-slate-50">
        <HelpCircle size={18} />
      </button>
      {#if canCreate}
        <button onclick={openCreate}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
          + New run
        </button>
      {/if}
    </div>
  </div>

  <!-- Filter bar -->
  <div class="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-white p-3">
    <!-- Search -->
    <div class="relative min-w-[220px] flex-1">
      <input type="search" bind:value={search} oninput={onSearchInput}
             placeholder="Search by name…"
             class="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
    </div>

    <!-- Status -->
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Status
      <select bind:value={filterStatus} onchange={() => refresh(false)}
              class="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-700">
        {#each STATUS_PRESETS as p}
          <option value={p.value}>{p.label}</option>
        {/each}
      </select>
    </label>

    <!-- Project -->
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Project
      <select bind:value={filterProjectId} onchange={() => refresh(false)}
              class="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-700">
        <option value={null}>All</option>
        {#each projectsForFilter as p}
          <option value={p.id}>{p.name}</option>
        {/each}
      </select>
    </label>

    <!-- Creator -->
    {#if usersForFilter.length > 0}
      <label class="flex items-center gap-1.5 text-xs text-slate-500">
        Creator
        <select bind:value={filterCreatorId} onchange={() => refresh(false)}
                class="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-700">
          <option value={null}>All</option>
          {#each usersForFilter as u}
            <option value={u.id}>@{u.username}</option>
          {/each}
        </select>
      </label>
    {/if}

    {#if anyFilterActive}
      <button type="button" onclick={clearFilters}
              class="ml-auto rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50">
        <X size={14} class="inline-block align-text-bottom" /> Clear filters
      </button>
    {/if}
    {#if isSuper}
      <label class="ml-3 flex items-center gap-1.5 text-xs text-slate-600"
             title="Показать soft-deleted раны (только super_admin)">
        <input type="checkbox" bind:checked={showDeleted} onchange={() => refresh(false)}
               class="rounded border-slate-300" />
        Показать удалённые
      </label>
    {/if}
  </div>

  <p class="text-xs text-slate-400">
    {#if initialLoading}Loading…{:else}Showing {items.length} run{items.length === 1 ? '' : 's'}{#if anyFilterActive} (filtered){/if}.{/if}
  </p>

  <!-- Global queue: видна всем юзерам, помогает распределять нагрузку.
       Показывается ВСЕГДА (даже когда пусто) — чтобы юзеры знали что панель есть. -->
  {#if true}
    {@const totalTexts = queue.reduce((sum, q) => sum + q.total_texts, 0)}
    {@const totalPosted = queue.reduce((sum, q) => sum + q.posted_count, 0)}
    {@const totalPending = totalTexts - totalPosted}
    <section class="rounded-lg border border-brand-200 bg-brand-50/30 p-4">
      <!-- Шапка: суммарная нагрузка по всей очереди -->
      <div class="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-brand-800">
          Global queue
        </h2>
        <div class="flex flex-wrap items-center gap-3 text-xs">
          <span class="text-slate-600"><strong class="text-slate-800">{queue.length}</strong> runs</span>
          <span class="text-slate-300">·</span>
          <span class="text-slate-600">
            <strong class="text-emerald-700 tabular-nums">{totalPosted}</strong>
            <span class="text-slate-400">/</span>
            <strong class="text-slate-800 tabular-nums">{totalTexts}</strong>
            posted
          </span>
          {#if totalPending > 0}
            <span class="text-slate-300">·</span>
            <span class="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
              <strong class="tabular-nums">{totalPending}</strong> pending
            </span>
          {/if}
          {#if linkChecks.length > 0}
            <span class="text-slate-300">·</span>
            <span class="rounded bg-violet-100 px-1.5 py-0.5 text-violet-700">
              <strong class="tabular-nums">{linkChecks.length}</strong> валидация ссылок
            </span>
          {/if}
        </div>
      </div>
      <p class="mb-2 text-[11px] text-slate-500">
        Все active runs (running / queued / paused / scheduled) по всем юзерам и проектам.
        Сервер крутит ограниченное число параллельно — координируйтесь по нагрузке.
      </p>
      {#if queue.length === 0 && linkChecks.length === 0}
        <div class="rounded-md bg-white/60 px-3 py-4 text-center text-xs text-slate-400">
          Очередь пуста — никто сейчас ничего не постит. Запустить run? Кнопка справа сверху.
        </div>
      {/if}
      <ol class="space-y-1.5">
        {#each queue as q, idx (q.id)}
          {@const gen = q.status === 'unpacking' && (q.gen_total ?? 0) > 0}
          {@const numer = gen ? (q.gen_done ?? 0) : q.posted_count}
          {@const denom = gen ? (q.gen_total ?? 0) : q.total_texts}
          {@const pct = denom > 0 ? (numer / denom) * 100 : 0}
          <li class="flex items-center gap-3 rounded-md px-2 py-1.5 text-sm transition"
              class:bg-white={q.is_mine}
              class:ring-1={q.is_mine}
              class:ring-brand-300={q.is_mine}>
            <span class="w-6 text-right text-xs font-mono text-slate-400">{idx + 1}</span>
            <a href={`/runs/${q.id}`} class="min-w-[120px] truncate font-medium text-slate-800 hover:text-brand-700 hover:underline">
              {q.name}
            </a>
            <span class="rounded-full px-2 py-0.5 text-[10px] font-medium uppercase
                         {gen ? 'bg-orange-100 text-orange-700' :
                          q.status === 'running' ? 'bg-emerald-100 text-emerald-700' :
                          q.status === 'paused' ? 'bg-amber-100 text-amber-700' :
                          q.status === 'scheduled' ? 'bg-slate-200 text-slate-600' :
                          'bg-slate-100 text-slate-600'}">
              {gen ? 'генерация' : q.status}
              {#if q.status === 'running' || gen}
                <span class="ml-0.5 inline-block h-1 w-1 animate-pulse rounded-full {gen ? 'bg-orange-500' : 'bg-emerald-600'}"></span>
              {/if}
            </span>
            <!-- Progress bar (визуальная пропорция). Генерация — оранжевый; постинг — зелёный. -->
            <div class="relative flex h-2 flex-1 items-center overflow-hidden rounded-full bg-slate-200"
                 title={gen ? `Генерация: ${numer} из ${denom}` : `${numer} posted out of ${denom}`}>
              {#if denom > 0}
                <div class="h-full {gen ? 'bg-orange-500' : 'bg-emerald-500'} transition-all" style="width: {pct}%"></div>
              {/if}
            </div>
            <span class="w-24 shrink-0 text-right text-xs tabular-nums text-slate-500">
              <strong class={gen ? 'text-orange-600' : 'text-emerald-700'}>{numer}</strong>
              <span class="text-slate-300">/</span>
              <strong class="text-slate-700">{denom}</strong>
              {#if gen}<span class="ml-0.5 text-[9px] uppercase text-orange-400">gen</span>{/if}
            </span>
            {#if q.is_mine}
              <span class="shrink-0 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-emerald-700">
                yours
              </span>
            {:else}
              <span class="w-16 shrink-0 truncate text-right text-[11px] text-slate-400" title={`@${q.creator_username ?? '?'} · ${q.project_name}`}>
                @{q.creator_username ?? '?'}
              </span>
            {/if}
          </li>
        {/each}
      </ol>
      {#if linkChecks.length > 0}
        <div class="mt-2 border-t border-violet-200/60 pt-2">
          <p class="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-violet-700">
            Валидация проставленных ссылок
          </p>
          <ol class="space-y-1.5">
            {#each linkChecks as lc (lc.id)}
              {@const pct = lc.total > 0 ? (lc.done / lc.total) * 100 : 0}
              <li class="flex items-center gap-3 rounded-md px-2 py-1.5 text-sm transition"
                  class:bg-white={lc.is_mine}
                  class:ring-1={lc.is_mine}
                  class:ring-violet-300={lc.is_mine}>
                <span class="w-6 text-right text-xs">🔎</span>
                <a href={`/runs/${lc.id}`} class="min-w-[120px] truncate font-medium text-slate-800 hover:text-violet-700 hover:underline">
                  {lc.name}
                </a>
                <span class="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium uppercase text-violet-700">
                  проверка ссылок
                  {#if lc.status === 'running'}
                    <span class="ml-0.5 inline-block h-1 w-1 animate-pulse rounded-full bg-violet-500"></span>
                  {/if}
                </span>
                <div class="relative flex h-2 flex-1 items-center overflow-hidden rounded-full bg-slate-200"
                     title={`Проверено ${lc.done} из ${lc.total}`}>
                  {#if lc.total > 0}
                    <div class="h-full bg-violet-500 transition-all" style="width: {pct}%"></div>
                  {/if}
                </div>
                <span class="w-24 shrink-0 text-right text-xs tabular-nums text-slate-500">
                  <strong class="text-violet-700">{lc.done}</strong>
                  <span class="text-slate-300">/</span>
                  <strong class="text-slate-700">{lc.total}</strong>
                  <span class="ml-0.5 text-[9px] uppercase text-violet-400">✓{lc.valid}</span>
                </span>
                {#if lc.is_mine}
                  <span class="shrink-0 rounded bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-violet-700">yours</span>
                {:else}
                  <span class="w-16 shrink-0 truncate text-right text-[11px] text-slate-400" title={`@${lc.creator_username ?? '?'} · ${lc.project_name}`}>
                    @{lc.creator_username ?? '?'}
                  </span>
                {/if}
              </li>
            {/each}
          </ol>
        </div>
      {/if}
    </section>
  {/if}

  <!-- Table -->
  {#if initialLoading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      {anyFilterActive ? 'No runs match the filter.' : 'Прогонов ещё не было.'}
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-4 py-2">ID</th>
            <th class="px-4 py-2">Run</th>
            <th class="px-4 py-2">Project</th>
            <th class="px-4 py-2">Creator</th>
            <th class="px-4 py-2">Status</th>
            <th class="px-4 py-2 text-center">Progress</th>
            <th class="px-4 py-2 text-center">Created</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as r (r.id)}
            {@const gen = r.status === 'unpacking' && (r.gen_total ?? 0) > 0}
            {@const pct = gen ? Math.round(((r.gen_done ?? 0) / (r.gen_total || 1)) * 100) : progressPct(r)}
            <tr class="hover:bg-slate-50">
              <td class="px-4 py-2 text-slate-500">{r.id}</td>
              <td class="px-4 py-2 font-medium text-slate-900">
                <a href={`/runs/${r.id}`} class="hover:text-brand-600 hover:underline">
                  {r.name}
                </a>
                <span class="mt-0.5 block text-[11px] font-normal text-indigo-600">{runModeLabel(r)}</span>
              </td>
              <td class="px-4 py-2 text-slate-600">
                <a href={`/projects/${r.project.id}`} class="text-brand-600 hover:underline">
                  {r.project.name}
                </a>
              </td>
              <td class="px-4 py-2 text-slate-600">@{r.creator?.username ?? '—'}</td>
              <td class="px-4 py-2">
                <div class="flex items-center gap-2">
                  <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusBadgeClass(r.status)}">
                    {r.status.replace('_', ' ')}
                  </span>
                  {#if r.deleted_at}
                    <span class="rounded-full bg-red-100 px-2 py-0.5 text-[11px] font-medium uppercase text-red-700"
                          title={`Удалено ${new Date(r.deleted_at).toLocaleString()}`}>удалено</span>
                    {#if r.deleted_by_user}<span class="text-[11px] text-slate-400">@{r.deleted_by_user.username}</span>{/if}
                    {#if isSuper}
                      <button onclick={() => restoreRun(r)}
                              class="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50">Restore</button>
                      <button onclick={() => purgeRun(r)}
                              class="rounded border border-red-300 px-2 py-0.5 text-[11px] text-red-600 hover:bg-red-50">Purge</button>
                    {/if}
                  {/if}
                  {#if r.status === 'ready' || r.status === 'scheduled'}
                    <button onclick={() => startRun(r)}
                            title={r.status === 'scheduled'
                              ? `Запустить НЕМЕДЛЕННО, не дожидаясь scheduled_for (${r.scheduled_for ? new Date(r.scheduled_for).toLocaleString() : ''})`
                              : 'Запустить постинг'}
                            class="inline-flex items-center gap-1 rounded bg-emerald-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-emerald-700">
                      <Play size={11} class="inline" /> Start
                    </button>
                  {/if}
                  {#if r.status === 'failed' || r.status === 'interrupted' || r.status === 'cancelled' || r.status === 'need_more_admins'}
                    <button onclick={() => restartRun(r)}
                            title="Сбросить failed/posting/skipped/pending → pending и запустить заново"
                            class="inline-flex items-center gap-1 rounded bg-emerald-600 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-emerald-700">
                      <Play size={11} class="inline" /> Restart
                    </button>
                  {/if}
                </div>
              </td>
              <!-- Прогресс: цифры прямо В баре (зелёный=пост/красный=fail|gen) -->
              <td class="px-4 py-2">
                {#if gen || r.total_texts > 0}
                  <div class="relative mx-auto h-5 w-32 overflow-hidden rounded bg-slate-100">
                    <div class="absolute inset-y-0 left-0 {gen ? 'bg-orange-200' : 'bg-emerald-200'} transition-all" style="width: {pct}%"></div>
                    <span class="absolute inset-0 flex items-center justify-center text-[11px] font-medium tabular-nums">
                      {#if gen}
                        <span class="text-orange-600">{r.gen_done ?? 0}</span><span class="text-slate-400">/{r.gen_total}</span>
                        <span class="ml-1 text-[9px] uppercase text-orange-400">gen</span>
                      {:else}
                        <span class="text-emerald-700">{r.posted_count}</span><span class="text-slate-400">/</span><span class="text-red-600">{r.failed_count}</span><span class="text-slate-400">/{r.total_texts}</span>
                      {/if}
                    </span>
                  </div>
                {:else}
                  <span class="text-xs text-slate-400">—</span>
                {/if}
              </td>
              <td class="px-4 py-2 text-center text-xs text-slate-500">
                {new Date(r.created_at).toLocaleString()}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if hasActiveRuns()}
      <p class="text-xs text-slate-400">Auto-refresh каждые 5 сек пока есть активные прогоны.</p>
    {/if}
  {/if}
</div>

<!-- Create run modal -->
{#if createOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Новый прогон</h2>

      <!-- Тип прогона -->
      <div class="mt-3 grid grid-cols-3 gap-1">
        {#each [['post', 'Пост'], ['sitewide_link', 'Сквозная'], ['homepage_link', 'С главной']] as [val, label]}
          {@const on = newTaskType === val}
          <button type="button" onclick={() => selectTaskType(val as 'post' | 'sitewide_link' | 'homepage_link')}
                  class="rounded-md border px-2 py-1.5 text-xs font-medium transition"
                  class:border-brand-600={on} class:bg-brand-50={on} class:text-brand-700={on}
                  class:border-slate-300={!on} class:text-slate-600={!on} class:hover:bg-slate-50={!on}>
            {label}
          </button>
        {/each}
      </div>

      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label for="nrr_proj" class="block text-sm font-medium text-slate-700">Project *</label>
            <select id="nrr_proj" bind:value={newProjectId} required onchange={refreshLinkCandidates}
                    class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
              {#each manageableProjects as p}
                <option value={p.id}>{p.name}</option>
              {/each}
            </select>
          </div>
          <div>
            <label for="nrr_name" class="block text-sm font-medium text-slate-700">Name *</label>
            <input id="nrr_name" type="text" bind:value={newName} required minlength="1" maxlength="255"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
        </div>

        {#if newTaskType === 'post'}
          <!-- источник текстов -->
          <div>
            <span class="block text-sm font-medium text-slate-700">Источник текстов</span>
            <div class="mt-1 grid grid-cols-3 gap-1">
              {#each [['zip', 'Архив .txt'], ['csv_direct', 'CSV тексты'], ['gen', 'Генерация / Reuse']] as [val, label]}
                {@const on = postSource === val}
                <button type="button" onclick={() => selectPostSource(val as 'zip' | 'csv_direct' | 'gen')}
                        class="rounded-md border px-2 py-1.5 text-xs font-medium transition"
                        class:border-brand-600={on} class:bg-brand-50={on} class:text-brand-700={on}
                        class:border-slate-300={!on} class:text-slate-600={!on} class:hover:bg-slate-50={!on}>{label}</button>
              {/each}
            </div>
            <p class="mt-1 text-[11px] text-slate-400">
              {#if postSource === 'zip'}.zip с .txt — каждый файл один пост (<code class="rounded bg-slate-100 px-1">&lt;title&gt;</code> = заголовок).
              {:else if postSource === 'csv_direct'}CSV/XLSX с готовыми текстами: столбцы anchor, link, text. По умолчанию тело постится как есть (ссылка должна быть уже в тексте); link/anchor — для валидации и аналитики. Инжект ссылки — галочкой ниже.
              {:else}CSV/XLSX anchor,link,count — тексты сгенерит AI или возьмём из библиотеки (reuse). Генерация отдельной полосой, не блокирует постинг.
              {/if}
            </p>
          </div>

          <!-- файл -->
          <div>
            <label for="nrr_file" class="block text-sm font-medium text-slate-700">
              {postSource === 'zip' ? 'Архив (.zip)' : 'Файл (.csv / .xlsx)'} *
            </label>
            <input id="nrr_file" type="file" required
                   accept={postSource === 'zip' ? '.zip,application/zip' : '.csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
                   onchange={(e) => applyFile((e.currentTarget as HTMLInputElement).files?.[0] ?? null)}
                   class="mt-1 w-full text-sm" />
            {#if newFile}<p class="mt-1 text-xs text-slate-500">{newFile.name} · {(newFile.size / 1024).toFixed(1)} KB</p>{/if}
          </div>

          <!-- csv_direct: инжект ссылки в тело (опционально) -->
          {#if postSource === 'csv_direct'}
            <label class="flex items-start gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
              <input type="checkbox" bind:checked={csvInjectLink} class="mt-0.5 rounded border-slate-300" />
              <span>
                <span class="font-medium text-slate-700">Инжектить ссылку в текст</span>
                <span class="mt-0.5 block text-[11px] leading-relaxed text-slate-500">
                  Вставить <code class="rounded bg-slate-100 px-1">link</code> с анкором
                  <code class="rounded bg-slate-100 px-1">anchor</code> в тело: обернём вхождение анкора
                  (или значимое слово), иначе добавим ссылку в содержательный абзац. Старые
                  <code class="rounded bg-slate-100 px-1">&lt;a&gt;</code> в тексте при этом вычищаются.
                  Выкл — тело постится как есть.
                </span>
              </span>
            </label>
          {/if}

          <!-- генерация/reuse — режим + AI -->
          {#if postSource === 'gen'}
            <div>
              <span class="block text-sm font-medium text-slate-700">Режим контента</span>
              <div class="mt-1 grid grid-cols-3 gap-1">
                {#each [['gen_per_post', 'Текст на пост'], ['gen_per_row', 'Текст на строку'], ['reuse', 'Reuse']] as [val, label]}
                  {@const on = campContentMode === val}
                  <button type="button" onclick={() => (campContentMode = val as 'gen_per_post' | 'gen_per_row' | 'reuse')}
                          class="rounded-md border px-2 py-1.5 text-xs font-medium transition"
                          class:border-brand-600={on} class:bg-brand-50={on} class:text-brand-700={on}
                          class:border-slate-300={!on} class:text-slate-600={!on} class:hover:bg-slate-50={!on}>{label}</button>
                {/each}
              </div>
              <p class="mt-1 text-[11px] text-slate-400">
                {#if campContentMode === 'gen_per_post'}Уникальный AI-текст на каждый из count постов.
                {:else if campContentMode === 'gen_per_row'}AI генерит 1 спинтакс-оригинал на строку. В Manual — сперва ревью оригиналов, на Start расшивается в count уникальных вариантов.
                {:else}Берём reusable-оригиналы из библиотеки (со spin_formula) и расшиваем в уникальные варианты. AI не нужен.
                {/if}
              </p>
            </div>

            {#if campContentMode !== 'reuse'}
              <div>
                <label for="nrr_key" class="block text-sm font-medium text-slate-700">Ключ / Провайдер *</label>
                {#if campProviders.length === 0}
                  <p class="mt-1 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] text-amber-700">
                    Нет доступных ключей с content-моделью. Добавь провайдера и модель на странице <b>AI Settings</b>.
                  </p>
                {:else}
                  <select id="nrr_key" bind:value={campProviderId} onchange={onCampKeyChange}
                          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    {#each campProviders as p}<option value={p.id}>{campProviderLabel[p.id] ?? p.name}</option>{/each}
                  </select>
                  <p class="mt-1 text-[11px] text-slate-400">Один ключ — несколько моделей. По умолчанию выбран ваш собственный.</p>
                {/if}
              </div>
              {#if campProviders.length > 0}
                <div>
                  <label for="nrr_model" class="block text-sm font-medium text-slate-700">AI-модель *</label>
                  <select id="nrr_model" bind:value={campModelId}
                          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    {#each campKeyModels as m}<option value={m.id}>{m.display_name} ({m.model_id})</option>{/each}
                  </select>
                </div>
              {/if}
              <div>
                <label for="nrr_prompt" class="block text-sm font-medium text-slate-700">Шаблон промпта</label>
                <select id="nrr_prompt" bind:value={campPromptId}
                        class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                  <option value={null}>— без шаблона ({'{keyword}'}) —</option>
                  {#each campPrompts as t}<option value={t.id}>{t.name}</option>{/each}
                </select>
              </div>
            {/if}

            <div class="flex items-center gap-3">
              <span class="text-sm font-medium text-slate-700">Запуск</span>
              {#each [['manual', 'Manual (ревью → Start)'], ['auto', 'Auto (сразу)']] as [val, label]}
                {@const on = campRunMode === val}
                <button type="button" onclick={() => (campRunMode = val as 'auto' | 'manual')}
                        class="rounded-md border px-2 py-1 text-xs font-medium transition"
                        class:border-brand-600={on} class:bg-brand-50={on} class:text-brand-700={on}
                        class:border-slate-300={!on} class:text-slate-600={!on}>{label}</button>
              {/each}
            </div>

            <div>
              <label for="nrr_lang" class="block text-sm font-medium text-slate-700">Язык текстов</label>
              <input id="nrr_lang" type="text" bind:value={campLang} maxlength="20" placeholder="English"
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
              <p class="mt-1 text-[11px] text-slate-400">
                {#if campContentMode === 'reuse'}На каком языке брать reusable-оригиналы из библиотеки.
                {:else}На каком языке AI пишет тексты (передаётся в промпт).
                {/if}
              </p>
            </div>
          {/if}
        {:else}
          <!-- link-режимы: файл anchor,link,count (count = на сколько сайтов) -->
          <p class="text-xs text-slate-500">
            {#if newTaskType === 'sitewide_link'}Сквозная ссылка в footer/header (на всех страницах) на admin-сайтах — через виджет/меню/шаблон, с проверкой что видна.
            {:else}Ссылка с главной: в контенте статической главной или в FSE-шаблоне главной, с проверкой.
            {/if}
            {#if linkCandidates !== null}<span class="font-medium text-blue-700"> Доступно admin-сайтов: {linkCandidates}.</span>{/if}
          </p>
          <div>
            <label for="nrr_lfile" class="block text-sm font-medium text-slate-700">Файл (.csv / .xlsx): anchor, link, count[, text] *</label>
            <input id="nrr_lfile" type="file" required
                   accept=".csv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                   onchange={(e) => applyFile((e.currentTarget as HTMLInputElement).files?.[0] ?? null)}
                   class="mt-1 w-full text-sm" />
            {#if newFile}<p class="mt-1 text-xs text-slate-500">{newFile.name} · {(newFile.size / 1024).toFixed(1)} KB</p>{/if}
            <div class="mt-3">
              <span class="block text-sm font-medium text-slate-700">Скрытие ссылки/текста (опц.)</span>
              <p class="text-[11px] text-slate-400">Оборачиваем контент в скрывающий <code>&lt;div&gt;</code> — ссылка остаётся в исходнике страницы (проверка проходит), но не видна посетителю. Выбрано несколько — на каждый сайт берём случайный метод.</p>
              <div class="mt-1.5 flex flex-wrap gap-1.5">
                {#each HIDE_METHODS as m}
                  {@const on = hideMethods.includes(m.key)}
                  <button type="button" onclick={() => toggleHide(m.key)} title={m.hint}
                          class="rounded-md border px-2 py-1 text-[11px] transition"
                          class:border-brand-500={on} class:bg-brand-50={on} class:text-brand-700={on}
                          class:border-slate-300={!on} class:text-slate-600={!on}>
                    {on ? '✓ ' : ''}{m.label}
                  </button>
                {/each}
              </div>
              {#if hideMethods.length === 0}
                <p class="mt-1 text-[11px] text-slate-400">Ничего не выбрано → <b>без скрытия</b>.</p>
              {/if}
            </div>
            <p class="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] text-amber-700">
              <b>count</b> = на сколько сайтов поставить (опц., по умолчанию 1). Опц. <b>text</b> — готовый HTML-сниппет со встроенной ссылкой (ставим как есть). Площадки — из пула admin-сайтов без пересечений. После создания — <b>Start</b>.
            </p>
          </div>
        {/if}

        <!-- ─── Дополнительно: 3 смысловые категории-аккордеона ─── -->

        <!-- 1. Пул сайтов и доступов -->
        <div class="rounded-md border border-slate-200">
          <button type="button" onclick={() => (secPoolOpen = !secPoolOpen)}
                  class="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left hover:bg-slate-50">
            <span class="flex items-center gap-1.5 text-sm font-medium text-slate-700">
              <span class="text-[10px] text-slate-400">{secPoolOpen ? '▼' : '▶'}</span> Пул сайтов и доступов
            </span>
            <span class="text-[11px] text-slate-400">{poolSummary}</span>
          </button>
          {#if secPoolOpen}
            <div class="space-y-3 border-t border-slate-100 px-3 py-3">
              <div>
                <span class="block text-sm font-medium text-slate-700">Фильтр по языку / TLD сайта</span>
                <div class="mt-1 grid grid-cols-2 gap-2">
                  <input bind:value={newSiteLangs} placeholder="lang: en,fr,de"
                         class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
                  <input bind:value={newSiteTlds} placeholder="tld: us,uk,au"
                         class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono" />
                </div>
                <p class="mt-1 text-[11px] text-slate-400">Только сайты с этим <b>языком</b> и <b>TLD</b> (через запятую). Пусто = все.</p>
                <label class="mt-2 flex cursor-pointer items-start gap-2 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-2">
                  <input type="checkbox" bind:checked={newPoolFallback} class="mt-0.5" />
                  <span class="text-[11px] text-slate-600">
                    <b>Авто-добор по всему пулу</b> — если доступы под фильтром (язык/TLD/теги) кончились,
                    не вставать в <code>need_more_admins</code>, а продолжить по остальному разрешённому пулу.
                    Сначала точно проставит по фильтру, потом доберёт остальным.
                  </span>
                </label>
              </div>

              <div>
                <span class="block text-sm font-medium text-slate-700">Пул доступов</span>
                <div class="mt-1 flex flex-wrap items-center gap-1.5">
                  <button type="button" onclick={() => (poolMode = poolMode === 'tags' ? 'all' : 'tags')}
                          class="rounded-full border px-3 py-1 text-xs font-medium {poolMode === 'tags' ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">
                    По тегам
                  </button>
                  <button type="button" onclick={() => (poolMode = poolMode === 'domains' ? 'all' : 'domains')}
                          class="rounded-full border px-3 py-1 text-xs font-medium {poolMode === 'domains' ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">
                    Свой список доменов
                  </button>
                  {#if poolMode === 'all'}<span class="text-[11px] text-slate-400">сейчас: весь пул</span>{/if}
                </div>

                {#if poolMode === 'tags'}
                  {#if availableTags.length === 0}
                    <p class="mt-2 text-[11px] text-slate-400">Тегов пока нет — добавь теги кредам/батчам.</p>
                  {:else}
                    {#if newSiteTags.length}
                      <div class="mt-2 flex flex-wrap items-center gap-1.5">
                        {#each newSiteTags as tag}
                          <button type="button" onclick={() => toggleTag(tag)}
                                  class="flex items-center gap-1 rounded-full border border-brand-400 bg-brand-50 px-2.5 py-1 text-[12px] text-brand-700">
                            {tag} <X size={11} class="inline-block" />
                          </button>
                        {/each}
                        <button type="button" onclick={() => (newSiteTags = [])} class="px-1 text-[11px] text-slate-400 hover:text-slate-600">сбросить</button>
                      </div>
                    {/if}
                    <input bind:value={tagSearch} placeholder={`поиск среди ${availableTags.length} тегов…`}
                           class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                    <div class="mt-2 flex max-h-32 flex-wrap gap-1.5 overflow-auto">
                      {#each tagResults as tag}
                        <button type="button" onclick={() => toggleTag(tag)}
                                class="rounded-full border border-slate-300 bg-white px-2.5 py-1 text-[12px] text-slate-600 hover:bg-slate-50">
                          + {tag}
                        </button>
                      {/each}
                      {#if tagResults.length === 0}
                        <p class="text-[11px] text-slate-400">{tagSearch.trim() ? 'Ничего не найдено.' : 'Все теги выбраны.'}</p>
                      {/if}
                    </div>
                    {#if tagResultsMore > 0}
                      <p class="mt-1 text-[11px] text-slate-400">…ещё {tagResultsMore} — уточни поиск.</p>
                    {/if}
                    <p class="mt-1 text-[11px] text-slate-400">Выбрано: <b>{newSiteTags.length}</b> · теги батчей; берём сайты с доступом из батча с одним из выбранных тегов.</p>
                  {/if}
                {:else if poolMode === 'domains'}
                  {#if newSiteDomainsKey}
                    <div class="mt-2 flex items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
                      <Check size={14} /> Загружено <b>{domainsFileCount}</b> доменов из файла.
                      <button type="button" onclick={clearDomainsFile} class="ml-auto text-[12px] text-emerald-700 hover:underline">убрать</button>
                    </div>
                  {:else}
                    <textarea bind:value={newSiteDomains} rows="4"
                              placeholder="по домену в строке (или через запятую): example.com, blog.example.org"
                              class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm font-mono"></textarea>
                    <div class="mt-2 flex items-center gap-2 text-[11px] text-slate-500">
                      <span>или большой список файлом:</span>
                      <input type="file" accept=".txt,.csv,text/plain,text/csv" disabled={domainsUploading}
                             onchange={(e) => uploadDomainsFile((e.currentTarget as HTMLInputElement).files?.[0] ?? null)}
                             class="text-[11px]" />
                      {#if domainsUploading}<span class="text-slate-400">загрузка…</span>{/if}
                    </div>
                  {/if}
                  <p class="mt-1 text-[11px] text-slate-400">
                    Постим только на эти домены — креды к ним берём из базы.
                    {#if domainCount > 0}<b>{domainCount}</b> домен(ов).{/if}
                  </p>
                {/if}
              </div>
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
            <span class="text-[11px] text-slate-400">{schedSummary}</span>
          </button>
          {#if secSchedOpen}
            <div class="grid grid-cols-2 gap-3 border-t border-slate-100 px-3 py-3">
              <div>
                <label for="nrr_sched" class="block text-sm font-medium text-slate-700">
                  Scheduled start <span class="text-slate-400">(пусто = без расписания)</span>
                </label>
                <input id="nrr_sched" type="datetime-local" bind:value={newScheduledFor}
                       class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
              </div>
              <div>
                <label for="nrr_spread" class="block text-sm font-medium text-slate-700">
                  Разбить на дней <span class="text-slate-400">(0 = сразу)</span>
                </label>
                <input id="nrr_spread" type="number" min="0" max="365" bind:value={newSpreadDays}
                       class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                <p class="mt-1 text-[11px] text-slate-400">Drip-feed: размазать постинг по окну на N дней (link velocity).</p>
              </div>
              <div class="col-span-2">
                <span class="block text-sm font-medium text-slate-700">
                  Окно публикации <span class="text-slate-400">(пусто = стандартное из настроек)</span>
                </span>
                <div class="mt-1 grid grid-cols-2 gap-2">
                  <input type="date" bind:value={newPublishFrom} max={newPublishTo || today} aria-label="Publish from"
                         class="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                  <input type="date" bind:value={newPublishTo} min={newPublishFrom || undefined} max={today} aria-label="Publish to"
                         class="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                </div>
                {#if newWindowInvalid}
                  <p class="mt-1 text-[11px] text-red-600">Заполни обе даты, From не позже To.</p>
                {:else if newWindowFuture}
                  <p class="mt-1 text-[11px] text-amber-600">
                    <AlertTriangle size={12} class="inline-block align-text-bottom" /> Дата позже сегодня — посты уйдут в Scheduled. Выбери не позже сегодняшней.
                  </p>
                {:else}
                  <p class="mt-1 text-[11px] text-slate-400">Каждому посту — случайная (прошедшая) дата внутри окна. Пусто = стандартное окно из настроек.</p>
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
            <span class="text-[11px] text-slate-400">{postSummary}</span>
          </button>
          {#if secPostOpen}
            <div class="space-y-3 border-t border-slate-100 px-3 py-3">
              <div>
                <span class="block text-sm font-medium text-slate-700">Priority</span>
                <div class="mt-1 flex gap-1">
                  {#each [['low', 'Low'], ['normal', 'Normal'], ['high', 'High']] as [val, label]}
                    {@const isOn = newPriority === val}
                    <button type="button" onclick={() => (newPriority = val as PostingRunPriority)}
                            class="flex-1 rounded-md border px-2 py-1.5 text-xs font-medium transition"
                            class:border-brand-600={isOn} class:bg-brand-50={isOn} class:text-brand-700={isOn}
                            class:border-slate-300={!isOn} class:text-slate-600={!isOn} class:hover:bg-slate-50={!isOn}>
                      {label}
                    </button>
                  {/each}
                </div>
                <p class="mt-1 text-[11px] text-slate-400">High идёт в работу раньше Normal/Low в очереди.</p>
              </div>

              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label for="nrr_mpps" class="block text-sm font-medium text-slate-700">Max posts / site</label>
                  <input id="nrr_mpps" type="number" min="1" max="1000" bind:value={newMaxPostsPerSite}
                         class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
                  <p class="mt-1 text-[11px] text-slate-400"><strong>1</strong> = «1 сайт = 1 пост». Подними, чтобы добрать из использованных.</p>
                </div>
                <div>
                  <label for="nrr_proxy" class="block text-sm font-medium text-slate-700">Proxy pool</label>
                  <select id="nrr_proxy" bind:value={newProxySelector}
                          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    {#if poolStats.all_active > 0}
                      <option value="all">All proxies ({poolStats.all_active} — round-robin)</option>
                      {#each Object.entries(poolStats.providers) as [name, cnt]}
                        {#if cnt > 0}
                          <option value={`provider:${name}`}>Provider: {name} ({cnt})</option>
                        {/if}
                      {/each}
                    {/if}
                    <option value="direct">— без прокси (direct) —</option>
                  </select>
                  <p class="mt-1 text-[11px] text-slate-400">Рандомизация прокси на каждый запрос. Один proxy = bottleneck на 1000+.</p>
                </div>
              </div>

              {#if newTaskType === 'post'}
                <div>
                  <label for="nrr_method" class="block text-sm font-medium text-slate-700">Posting method</label>
                  <select id="nrr_method" bind:value={newPostingMethod}
                          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    <option value="auto">Auto — XML-RPC → wp-admin fallback (recommended)</option>
                    <option value="xmlrpc_only">XML-RPC only — fastest, classic</option>
                    <option value="admin_only">wp-admin only — for sites with XML-RPC disabled</option>
                  </select>
                  <p class="mt-1 text-[11px] text-slate-400">Auto ловит ~50% больше cred-ов (где XML-RPC выключен, но wp-admin работает).</p>
                </div>
                <div>
                  <label for="nrr_verify" class="block text-sm font-medium text-slate-700">Валидация ссылки на посте</label>
                  <select id="nrr_verify" bind:value={newPostVerify}
                          class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
                    <option value="mark">Отметка — проверить и пометить ✓/✗, пост засчитан в любом случае</option>
                    <option value="auto">Автовалидация — перепост, пока ссылка не подтвердится на странице</option>
                  </select>
                  <p class="mt-1 text-[11px] text-slate-400"><b>Отметка</b> — только помечает. <b>Автовалидация</b> — не готова, пока ссылка не подтвердится (перепост на другой сайт).</p>
                </div>
              {/if}
            </div>
          {/if}
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Cancel
          </button>
          <button type="submit" disabled={createBusy || !newProjectId || !newFile || newWindowInvalid || newWindowFuture}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {createBusy ? 'Creating…' : (newTaskType === 'post' ? 'Create run' : 'Создать link-run')}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Help: режимы работы + форматы файлов (единая инструкция, переключатель) -->
{#if modesOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (modesOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg bg-white shadow-xl" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-start justify-between border-b border-slate-100 px-6 py-4">
        <div>
          <h2 class="text-lg font-semibold text-slate-900">Режимы работы и форматы</h2>
          <p class="mt-0.5 text-[12px] text-slate-500">Выбери режим — что он делает и какой файл нужен.</p>
        </div>
        <button type="button" onclick={() => (modesOpen = false)} class="text-slate-400 hover:text-slate-700">✕</button>
      </div>

      <!-- Переключатель режимов -->
      <div class="flex flex-wrap gap-1 border-b border-slate-100 px-6 pt-3">
        {#each [['post', '📝 Посты'], ['sitewide', '🔗 Сквозная'], ['homepage', '🏠 С главной'], ['common', '⚙️ Общее']] as [val, label]}
          {@const on = modesTab === val}
          <button type="button" onclick={() => (modesTab = val as 'post' | 'sitewide' | 'homepage' | 'common')}
                  class="rounded-t-md border-b-2 px-3 py-2 text-sm font-medium transition"
                  class:border-brand-600={on} class:text-brand-700={on}
                  class:border-transparent={!on} class:text-slate-500={!on} class:hover:text-slate-800={!on}>
            {label}
          </button>
        {/each}
      </div>

      <div class="overflow-auto px-6 py-5 text-sm text-slate-700">
        {#if modesTab === 'post'}
          <p class="text-slate-600">
            Публикует <b>обычные посты (статьи)</b> на WP-сайтах из пула проекта — каждый пост на отдельном сайте.
            Внутри бэклинк на твою target-ссылку под анкором. 3 источника текста:
          </p>

          <div class="mt-3 space-y-3">
            <div class="rounded-md border border-slate-200 p-3">
              <p class="font-semibold text-slate-900">1. Архив .txt</p>
              <p class="mt-1 text-slate-600">
                Готовые тексты в .zip (1 файл = 1 пост). Заголовок — из <code class="rounded bg-slate-100 px-1">&lt;title&gt;</code>
                или имени файла. Ссылку/анкор вытаскиваем <b>из самого текста</b> (домен сверяем с целевыми
                доменами проекта). Ничего не генерим — публикуем как есть.
              </p>
              <p class="mt-2 text-[12px] font-medium text-slate-500">Формат</p>
              <p class="mt-0.5 text-slate-600">
                ZIP с .txt; заголовок тегом <code class="rounded bg-slate-100 px-1">&lt;title&gt;Заголовок&lt;/title&gt;</code>,
                бэклинк прямо в тексте: <code class="rounded bg-slate-100 px-1">&lt;a href="https://target.com/"&gt;анкор&lt;/a&gt;</code>.
              </p>
              <pre class="mt-1.5 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">posts.zip → 1.txt, 2.txt, …</pre>
              <p class="mt-1 text-[12px] text-slate-500">Целевой домен ссылки должен быть в «Целевых доменах проекта» — иначе задача уйдёт в <i>needs_review</i> (дозаполнишь вручную).</p>
            </div>

            <div class="rounded-md border border-slate-200 p-3">
              <p class="font-semibold text-slate-900">2. CSV с текстами</p>
              <p class="mt-1 text-slate-600">
                Текст задан напрямую. На каждую строку 1 пост: чистим старые ссылки из текста и инжектим твою
                <code>link</code> под <code>anchor</code>. Без AI и без библиотеки.
              </p>
              <p class="mt-2 text-[12px] font-medium text-slate-500">Формат · CSV / XLSX</p>
              <p class="mt-0.5 text-slate-600">Столбцы <code class="rounded bg-slate-100 px-1">anchor, link, text</code>.</p>
              <pre class="mt-1.5 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">anchor,link,text
Nawal,https://nawal.mx/,"&lt;p&gt;Полный текст поста…&lt;/p&gt;"</pre>
            </div>

            <div class="rounded-md border border-slate-200 p-3">
              <p class="font-semibold text-slate-900">3. CSV с генерацией</p>
              <p class="mt-1 text-slate-600">
                На строку создаётся <code>count</code> постов; тексты пишет AI (или берём из библиотеки). 3 режима контента:
              </p>
              <ul class="mt-2 space-y-1.5 text-slate-600">
                <li class="rounded bg-slate-50 px-2 py-1.5">
                  <b class="text-slate-800">Текст на пост</b> — уникальный AI-текст для <i>каждого</i> из count постов
                  (максимум уникальности, больше расход модели).
                </li>
                <li class="rounded bg-slate-50 px-2 py-1.5">
                  <b class="text-slate-800">Текст на строку</b> — AI пишет 1 спинтакс-оригинал на строку, затем он
                  расшивается в count уникальных вариантов (спинов). Можно сперва проверить оригиналы, потом
                  «Заполнить спины» и запостить.
                </li>
                <li class="rounded bg-slate-50 px-2 py-1.5">
                  <b class="text-slate-800">Reuse</b> — берём готовые reusable-оригиналы из библиотеки (со spin_formula)
                  и расшиваем в варианты. AI не нужен (<code>keyword</code> тоже).
                </li>
              </ul>
              <p class="mt-2 text-[12px] font-medium text-slate-500">Формат · CSV / XLSX</p>
              <p class="mt-0.5 text-slate-600">
                Столбцы <code class="rounded bg-slate-100 px-1">anchor, link, count</code> (+ опц. <code>keyword</code>,
                <code>language</code>). <code>count</code> = постов на строку, <code>keyword</code> = тема для AI
                (переменная <code>{'{keyword}'}</code> в промпте).
              </p>
              <pre class="mt-1.5 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">anchor,link,count,keyword,language
Nawal,https://nawal.mx/,5,casino,English</pre>
              <p class="mt-2 text-[12px] text-slate-500">
                <b>Запуск:</b> Manual — генерация → ревью → «Старт постинга» (постинг можно запустить параллельно с
                генерацией: постится то, что уже готово). Auto — генерация и постинг идут потоком сразу.
              </p>
            </div>
          </div>
        {:else if modesTab === 'sitewide'}
          <p class="text-slate-600">
            Ставит <b>одну сквозную ссылку</b> — в footer / header / виджете / меню, то есть <b>на всех страницах</b>
            сайта (sitewide). Не создаёт пост — только размещает бэклинк.
          </p>
          <div class="mt-3 space-y-2">
            <ul class="list-disc space-y-1 pl-5 text-slate-600">
              <li>Площадки берём из пула <b>admin</b>-сайтов проекта без пересечений (нужен доступ в wp-admin, не просто XML-RPC).</li>
              <li>Размещение через wp-admin (виджет/меню/шаблон), затем <b>проверяем</b>, что ссылка есть в исходнике страницы.</li>
              <li>Опц. <b>скрытие</b> — прячем ссылку/текст от посетителя, но не из исходника (см. блок ниже).</li>
              <li>После создания — кнопка <b>Start</b>. Можно снять размещённую ссылку позже (в деталях прогона).</li>
            </ul>
            <div class="rounded-md border border-slate-200 p-3">
              <p class="text-[12px] font-medium text-slate-500">Формат · CSV / XLSX</p>
              <p class="mt-0.5 text-slate-600">
                Столбцы <code class="rounded bg-slate-100 px-1">anchor, link, count</code> + опц.
                <code class="rounded bg-slate-100 px-1">text</code>. <code>count</code> =
                <b>на сколько сайтов</b> поставить (опц., по умолчанию 1).
              </p>
              <ul class="mt-1.5 list-disc space-y-1 pl-5 text-[12px] text-slate-600">
                <li><b>Обычный режим</b> — заданы <code>anchor</code> + <code>link</code>: сами оборачиваем в <code>&lt;a href=link&gt;anchor&lt;/a&gt;</code>.</li>
                <li><b>Готовый HTML</b> — задан <code>text</code>: ставим этот сниппет (текст со встроенной ссылкой и тегами) <b>как есть</b>. <code>link</code> можно оставить пустым — вытащим первый href из текста для проверки.</li>
              </ul>
              <pre class="mt-1.5 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">anchor,link,count,text
1Go,https://1go-slots.com/,10,
,https://1go-slots.com/,,&lt;p&gt;Играй на &lt;a href="https://1go-slots.com/"&gt;1Go&lt;/a&gt; сегодня&lt;/p&gt;</pre>
              <p class="mt-1 text-[11px] text-slate-400">Сниппет с запятыми — в кавычках; в XLSX проще (просто ячейка).</p>
            </div>
            <div class="rounded-md border border-slate-200 p-3">
              <p class="text-[12px] font-medium text-slate-500">Скрытие ссылки/текста (опц.)</p>
              <p class="mt-0.5 text-slate-600">
                Оборачиваем контент (<b>текст</b>, если задан; иначе <b>ссылку</b> <code>&lt;a href&gt;</code>) в
                скрывающий <code>&lt;div&gt;</code>. Ссылка остаётся <b>в исходнике</b> страницы (наша проверка её
                находит), но <b>не видна</b> посетителю. Выбрано несколько методов — на каждый сайт берём
                <b>случайный</b> (разнообразит footprint).
              </p>
              <ul class="mt-1.5 list-disc space-y-1 pl-5 text-[12px] text-slate-600">
                <li><b>Без скрытия</b> — ничего не выбрано: обычная видимая ссылка.</li>
                <li><code>1px (sr-only)</code> — <code>position:absolute</code> + клип в 1px; классический a11y-хайд.</li>
                <li><code>display:none</code> — элемент убран из разметки, соседи занимают место.</li>
                <li><code>visibility:hidden</code> — невидим, но место держит.</li>
                <li><code>opacity:0</code> — прозрачен (кликабелен), место держит.</li>
                <li><code>hidden</code> — HTML-атрибут <code>&lt;div hidden&gt;</code>, аналог <code>display:none</code>.</li>
                <li><code>за экран</code> — <code>position:absolute; left:-99999px</code>.</li>
              </ul>
              <p class="mt-1.5 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
                ⚠ Скрытые ссылки — против правил Google (может привести к санкции на сайт-донор). Используй осознанно.
              </p>
            </div>
          </div>
        {:else if modesTab === 'homepage'}
          <p class="text-slate-600">
            Ставит ссылку <b>на главной странице</b> сайта — в контенте статической главной или в FSE-шаблоне
            главной. Тоже не пост, а размещение бэклинка, но только на home page (не sitewide).
          </p>
          <div class="mt-3 space-y-2">
            <ul class="list-disc space-y-1 pl-5 text-slate-600">
              <li>Площадки — из пула <b>admin</b>-сайтов проекта без пересечений.</li>
              <li>Правим контент/шаблон главной через wp-admin и <b>проверяем</b> наличие ссылки в исходнике.</li>
              <li>Опц. <b>скрытие</b> — прячем ссылку/текст от посетителя, но не из исходника (см. блок ниже). Работает и здесь, и в «Сквозной».</li>
              <li>После создания — кнопка <b>Start</b>.</li>
            </ul>
            <div class="rounded-md border border-slate-200 p-3">
              <p class="text-[12px] font-medium text-slate-500">Формат · CSV / XLSX</p>
              <p class="mt-0.5 text-slate-600">
                Столбцы <code class="rounded bg-slate-100 px-1">anchor, link, count</code> + опц.
                <code class="rounded bg-slate-100 px-1">text</code>. <code>count</code> =
                на сколько сайтов поставить (опц., по умолчанию 1).
              </p>
              <ul class="mt-1.5 list-disc space-y-1 pl-5 text-[12px] text-slate-600">
                <li><b>Обычный режим</b> — заданы <code>anchor</code> + <code>link</code>: сами оборачиваем в <code>&lt;a href=link&gt;anchor&lt;/a&gt;</code>.</li>
                <li><b>Готовый HTML</b> — задан <code>text</code>: ставим сниппет (текст со встроенной ссылкой) <b>как есть</b>; <code>link</code> опц. (вытащим href из текста).</li>
              </ul>
              <pre class="mt-1.5 overflow-auto rounded bg-slate-50 p-2 text-[11px] text-slate-600">anchor,link,count,text
1Go,https://1go-slots.com/,10,
,https://1go-slots.com/,,&lt;p&gt;Обзор &lt;a href="https://1go-slots.com/"&gt;1Go&lt;/a&gt;&lt;/p&gt;</pre>
            </div>
            <div class="rounded-md border border-slate-200 p-3">
              <p class="text-[12px] font-medium text-slate-500">Скрытие ссылки/текста (опц.)</p>
              <p class="mt-0.5 text-slate-600">
                Оборачиваем контент (<b>текст</b>, если задан; иначе <b>ссылку</b> <code>&lt;a href&gt;</code>) в
                скрывающий <code>&lt;div&gt;</code>. Ссылка остаётся <b>в исходнике</b> страницы (наша проверка её
                находит), но <b>не видна</b> посетителю. Выбрано несколько методов — на каждый сайт берём
                <b>случайный</b> (разнообразит footprint).
              </p>
              <ul class="mt-1.5 list-disc space-y-1 pl-5 text-[12px] text-slate-600">
                <li><b>Без скрытия</b> — ничего не выбрано: обычная видимая ссылка.</li>
                <li><code>1px (sr-only)</code> — <code>position:absolute</code> + клип в 1px; классический a11y-хайд.</li>
                <li><code>display:none</code> — элемент убран из разметки, соседи занимают место.</li>
                <li><code>visibility:hidden</code> — невидим, но место держит.</li>
                <li><code>opacity:0</code> — прозрачен (кликабелен), место держит.</li>
                <li><code>hidden</code> — HTML-атрибут <code>&lt;div hidden&gt;</code>, аналог <code>display:none</code>.</li>
                <li><code>за экран</code> — <code>position:absolute; left:-99999px</code>.</li>
              </ul>
              <p class="mt-1.5 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
                ⚠ Скрытые ссылки — против правил Google (может привести к санкции на сайт-донор). Используй осознанно.
              </p>
            </div>
          </div>
        {:else}
          <p class="text-slate-600">Общее для всех файлов (CSV / XLSX):</p>
          <ul class="mt-3 list-disc space-y-1.5 pl-5 text-slate-600">
            <li>Заголовки регистронезависимы; принимаются синонимы: <code>links/url → link</code>, <code>anchors → anchor</code>, <code>counts → count</code>, <code>keywords/kw → keyword</code>, <code>lang → language</code>.</li>
            <li>CSV в UTF-8 (разделитель — запятая) или XLSX (берётся первый лист, первая строка — заголовки).</li>
            <li>Минимум для распознавания: тексты — <code class="rounded bg-slate-100 px-1">anchor, link, text</code>; кампания — <code class="rounded bg-slate-100 px-1">anchor, link, count</code>; сквозная/с главной — <code class="rounded bg-slate-100 px-1">anchor, link, count</code> + опц. <code class="rounded bg-slate-100 px-1">text</code> (готовый HTML-сниппет).</li>
            <li>Прокси, расписание, drip и фильтр пула сайтов — в блоке «Дополнительно» формы создания.</li>
          </ul>
        {/if}
      </div>

      <div class="flex justify-end border-t border-slate-100 px-6 py-4">
        <button type="button" onclick={() => (modesOpen = false)}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Понятно</button>
      </div>
    </div>
  </div>
{/if}

