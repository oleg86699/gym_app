<script lang="ts">
  import { ArrowRight, Copy, HelpCircle, Wand2, Zap } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { proxies as proxApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import DropdownMenu from '$lib/components/ui/DropdownMenu.svelte'
  import type {
    Proxy,
    ProxyProviderStat,
    ProxySourceField,
    ProxySourceMeta,
  } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let items = $state<Proxy[]>([])
  let providers = $state<ProxyProviderStat[]>([])
  let sources = $state<ProxySourceMeta[]>([])
  let loading = $state(true)

  // Filters
  let search = $state('')
  let filterStatus = $state<'all' | 'active' | 'down' | 'unknown'>('all')
  let filterProvider = $state<string>('')

  let helpOpen = $state(false)   // инструкция по странице

  // Check all progress
  let checkBusy = $state(false)
  let checkProgress = $state<{ done: number; total: number } | null>(null)

  async function refresh() {
    loading = true
    try {
      const [list, prov] = await Promise.all([
        proxApi.list({
          search: search || undefined,
          status: filterStatus === 'all' ? undefined : filterStatus,
          provider: filterProvider || undefined,
          limit: 500,
        }),
        proxApi.providers(),
      ])
      items = list.items
      providers = prov
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadSources() {
    try {
      sources = await proxApi.sources()
    } catch {
      sources = []
    }
  }

  onMount(async () => {
    await refresh()
    await loadSources()
  })

  function statusColor(s: string): string {
    switch (s) {
      case 'active':
        return 'bg-emerald-100 text-emerald-700'
      case 'down':
        return 'bg-red-100 text-red-700'
      default:
        return 'bg-slate-100 text-slate-500'
    }
  }

  function typeBadge(t: string | null): string {
    switch (t) {
      case 'residential':
        return 'bg-emerald-100 text-emerald-700'
      case 'mobile':
        return 'bg-purple-100 text-purple-700'
      case 'datacenter':
        return 'bg-slate-200 text-slate-700'
      case 'proxy':
        return 'bg-amber-100 text-amber-700'
      default:
        return 'bg-slate-100 text-slate-500'
    }
  }

  function proxyEndpoint(p: Proxy): string {
    return `${p.protocol}://${p.host}:${p.port}`
  }

  async function doCheck(id: number) {
    try {
      const res = await proxApi.check(id)
      if (res.ok) showToast('success', `OK → ${res.external_ip} (${res.country ?? '—'})`)
      else showToast('error', res.error || 'check failed')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function doDelete(p: Proxy) {
    if (!confirm(`Delete proxy ${proxyEndpoint(p)}?`)) return
    try {
      await proxApi.remove(p.id)
      showToast('success', 'Deleted')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function checkAll() {
    if (checkBusy) return
    if (!confirm(`Запустить health-check для ${items.length} proxy? Это может занять несколько минут.`)) return
    checkBusy = true
    checkProgress = { done: 0, total: items.length }
    // Параллельно по 5 — балансим скорость и нагрузку
    const ids = items.map((p) => p.id)
    const CONCURRENCY = 5
    let cursor = 0
    async function worker() {
      while (cursor < ids.length) {
        const i = cursor++
        try {
          await proxApi.check(ids[i])
        } catch {
          // continue
        }
        if (checkProgress) checkProgress.done++
      }
    }
    await Promise.all(Array.from({ length: CONCURRENCY }, worker))
    checkBusy = false
    checkProgress = null
    showToast('success', 'Check completed')
    await refresh()
  }

  async function removeSource(source: string) {
    if (!confirm(`Удалить все proxy провайдера "${source}"? Их останется 0.`)) return
    try {
      const res = await proxApi.removeSource(source)
      showToast('success', `Removed ${res.deleted} proxies`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // ─── Manual add modal ──────────────────────────────────────────────

  let addOpen = $state(false)
  let m_protocol = $state('http')
  let m_host = $state('')
  let m_port = $state(8080)
  let m_username = $state('')
  let m_password = $state('')
  let m_country = $state('')
  let m_provider = $state('')
  let m_proxy_type = $state('')
  let m_note = $state('')

  function openAdd() {
    m_protocol = 'http'
    m_host = ''
    m_port = 8080
    m_username = ''
    m_password = ''
    m_country = ''
    m_provider = ''
    m_proxy_type = ''
    m_note = ''
    addOpen = true
  }

  async function submitAdd(e: SubmitEvent) {
    e.preventDefault()
    try {
      await proxApi.create({
        protocol: m_protocol,
        host: m_host,
        port: m_port,
        username: m_username || undefined,
        password: m_password || undefined,
        country: m_country || undefined,
        provider: m_provider || undefined,
        proxy_type: m_proxy_type || undefined,
        note: m_note || undefined,
      })
      showToast('success', 'Proxy added')
      addOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  // ─── Bulk add modal ────────────────────────────────────────────────

  let bulkOpen = $state(false)
  let bulkText = $state('')
  let bulkBusy = $state(false)

  function openBulk() {
    bulkText = ''
    bulkOpen = true
  }

  async function submitBulk(e: SubmitEvent) {
    e.preventDefault()
    bulkBusy = true
    try {
      const res = await proxApi.bulk(bulkText)
      showToast(
        res.invalid.length > 0 ? 'error' : 'success',
        `Parsed ${res.parsed}, inserted ${res.inserted}` +
          (res.invalid.length ? `, ${res.invalid.length} invalid lines` : ''),
      )
      bulkOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      bulkBusy = false
    }
  }

  // ─── Import from provider modal ────────────────────────────────────

  let importOpen = $state(false)
  let selectedProvider = $state<string>('')
  let importOpts = $state<Record<string, string>>({})
  let importBusy = $state(false)

  let selectedSource = $derived.by(() => sources.find((s) => s.name === selectedProvider) ?? null)

  function openImport(initialProvider?: string) {
    selectedProvider = initialProvider ?? sources[0]?.name ?? ''
    resetImportOpts()
    importOpen = true
  }

  function resetImportOpts() {
    const fresh: Record<string, string> = {}
    if (selectedSource) {
      for (const f of selectedSource.fields) {
        fresh[f.name] = f.default ?? ''
      }
    }
    importOpts = fresh
  }

  // При смене провайдера — заново подставить дефолты
  let prevProvider = $state('')
  $effect(() => {
    if (selectedProvider !== prevProvider) {
      prevProvider = selectedProvider
      resetImportOpts()
    }
  })

  async function submitImport(e: SubmitEvent) {
    e.preventDefault()
    if (!selectedSource) return
    importBusy = true
    try {
      const res = await proxApi.importFromSource(selectedProvider, importOpts)
      showToast(
        'success',
        `${selectedSource.display_name}: created ${res.created}, updated ${res.updated} (total ${res.total_in_db})`,
      )
      importOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      importBusy = false
    }
  }

  function fieldValue(field: ProxySourceField): string {
    return importOpts[field.name] ?? ''
  }

  function setFieldValue(field: ProxySourceField, value: string) {
    importOpts = { ...importOpts, [field.name]: value }
  }
</script>

<div class="space-y-4">
  <div class="flex items-start justify-between gap-4">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Proxies ({items.length})</h1>
      <p class="mt-1 text-sm text-slate-500">
        HTTP/SOCKS5 прокси для постинга. Хранятся зашифрованными, привязываются к прогону через
        <a href="/runs" class="text-brand-600 hover:underline">/runs</a> <ArrowRight size={14} class="inline-block align-text-bottom" /> New run.
      </p>
    </div>
    <div class="flex items-center gap-2">
      <button type="button" onclick={() => (helpOpen = true)}
              title="Инструкция: как добавлять прокси, колонки, статусы, проверка"
              aria-label="Инструкция по странице"
              class="inline-flex items-center justify-center rounded-md border border-slate-300 p-1.5 text-slate-600 hover:bg-slate-50">
        <HelpCircle size={18} />
      </button>
      <button onclick={checkAll} disabled={checkBusy || items.length === 0}
              class="rounded-md bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50">
        <Zap size={14} class="inline-block align-text-bottom" /> Check All
      </button>
      <button onclick={() => openImport()}
              disabled={sources.length === 0}
              class="rounded-md border border-purple-300 bg-purple-50 px-3 py-1.5 text-sm font-medium text-purple-800 hover:bg-purple-100 disabled:opacity-50">
        <Wand2 size={14} class="inline-block align-text-bottom" /> Import from Provider
      </button>
      <button onclick={openBulk}
              class="rounded-md border border-slate-300 bg-slate-50 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100">
        <Copy size={14} class="inline-block align-text-bottom" /> Bulk Add
      </button>
      <button onclick={openAdd}
              class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
        + Add Proxy
      </button>
    </div>
  </div>

  <!-- Providers section -->
  {#if providers.length > 0}
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="mb-2 text-xs font-medium uppercase tracking-wider text-slate-500">Providers</div>
      <div class="flex flex-wrap gap-2">
        {#each providers as p}
          {@const meta = sources.find((s) => s.name === p.source)}
          <div class="flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-1.5 text-sm">
            <span class="font-medium text-slate-800">{meta?.display_name ?? p.source}</span>
            <span class="rounded-full bg-white px-2 py-0.5 text-xs font-semibold text-slate-600">{p.count}</span>
            {#if meta}
              <button type="button" onclick={() => openImport(p.source)}
                      class="text-xs text-brand-600 hover:underline">Re-import</button>
            {/if}
            <button type="button" onclick={() => removeSource(p.source)}
                    class="text-xs text-red-600 hover:underline">Remove all</button>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Check progress -->
  {#if checkBusy && checkProgress}
    {@const pct = checkProgress.total ? Math.round((checkProgress.done / checkProgress.total) * 100) : 0}
    <div class="rounded-lg border border-emerald-200 bg-emerald-50/40 px-4 py-3 text-sm">
      <div class="flex items-center justify-between">
        <span><strong class="text-emerald-800">Checking proxies…</strong> {checkProgress.done} / {checkProgress.total}</span>
        <span>{pct}%</span>
      </div>
      <div class="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-200">
        <div class="h-full bg-emerald-500 transition-all" style="width: {pct}%"></div>
      </div>
    </div>
  {/if}

  <!-- Filters -->
  <div class="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-white p-3">
    <input type="search" bind:value={search}
           placeholder="Search host / IP / ISP / country…"
           oninput={() => refresh()}
           class="min-w-[260px] flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Status
      <select bind:value={filterStatus} onchange={() => refresh()}
              class="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value="all">All</option>
        <option value="active">Active</option>
        <option value="down">Down</option>
        <option value="unknown">Unknown</option>
      </select>
    </label>
    <label class="flex items-center gap-1.5 text-xs text-slate-500">
      Provider
      <select bind:value={filterProvider} onchange={() => refresh()}
              class="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value="">All</option>
        {#each providers as p}
          <option value={p.source}>{p.source}</option>
        {/each}
      </select>
    </label>
  </div>

  <!-- Table -->
  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      Нет proxy. Добавь вручную, bulk-ом или импортом от провайдера.
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-3 py-2">Server</th>
            <th class="px-3 py-2">Type</th>
            <th class="px-3 py-2">External IP</th>
            <th class="px-3 py-2">ISP / ASN</th>
            <th class="px-3 py-2">Country</th>
            <th class="px-3 py-2">Source</th>
            <th class="px-3 py-2">Status</th>
            <th class="px-3 py-2">Last check</th>
            <th class="px-3 py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as p (p.id)}
            <tr class="align-top hover:bg-slate-50">
              <td class="px-3 py-2 font-mono text-xs">
                <div class="text-slate-800">{proxyEndpoint(p)}</div>
                {#if p.username}<div class="text-slate-400">{p.username}</div>{/if}
              </td>
              <td class="px-3 py-2">
                <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {typeBadge(p.proxy_type)}">
                  {p.proxy_type ?? '—'}
                </span>
              </td>
              <td class="px-3 py-2 font-mono text-xs text-slate-700">{p.external_ip ?? '—'}</td>
              <td class="px-3 py-2 text-xs">
                <div class="text-slate-800">{p.isp ?? '—'}</div>
                <div class="text-slate-400">{p.asn ?? ''}</div>
              </td>
              <td class="px-3 py-2 text-xs font-semibold text-slate-700">{p.country ?? '—'}</td>
              <td class="px-3 py-2 text-xs">
                <span class="rounded-md bg-slate-100 px-1.5 py-0.5">{p.source}</span>
              </td>
              <td class="px-3 py-2">
                <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusColor(p.status)}">
                  {p.status}
                </span>
                {#if p.last_check_error}
                  <div class="mt-0.5 max-w-[200px] truncate text-[11px] text-red-600" title={p.last_check_error}>
                    {p.last_check_error}
                  </div>
                {/if}
              </td>
              <td class="px-3 py-2 text-xs text-slate-500">
                {p.last_checked_at ? new Date(p.last_checked_at).toLocaleString() : '—'}
              </td>
              <td class="px-3 py-2 text-right">
                <div class="inline-flex gap-2">
                  <button onclick={() => doCheck(p.id)} class="text-xs text-brand-600 hover:underline">Check</button>
                  <button onclick={() => doDelete(p)} class="text-xs text-red-600 hover:underline">Delete</button>
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<!-- Help: инструкция по странице -->
{#if helpOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (helpOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg bg-white shadow-xl" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-start justify-between border-b border-slate-100 px-6 py-4">
        <h2 class="text-lg font-semibold text-slate-900">Как работают прокси</h2>
        <button type="button" onclick={() => (helpOpen = false)} class="text-slate-400 hover:text-slate-700">✕</button>
      </div>

      <div class="space-y-4 overflow-auto px-6 py-5 text-sm text-slate-700">
        <p class="text-slate-600">
          Пул HTTP/SOCKS5 прокси для постинга и валидации. Хранятся <b>зашифрованными</b>. Привязываются к прогону
          в <a href="/runs" class="text-brand-600 hover:underline">/runs</a>
          <ArrowRight size={13} class="inline-block align-text-bottom" /> New run
          <ArrowRight size={13} class="inline-block align-text-bottom" /> «Дополнительно» (можно весь пул round-robin или один прокси).
        </p>

        <section>
          <h3 class="font-semibold text-slate-900">Как добавить — 3 способа</h3>
          <ul class="mt-1.5 space-y-1 text-slate-600">
            <li><b>+ Add Proxy</b> — вручную один (протокол, host, port, опц. логин/пароль, страна, тип).</li>
            <li><b>Bulk Add</b> — вставить списком, по одному в строке. Форматы:
              <code class="rounded bg-slate-100 px-1">host:port</code>,
              <code class="rounded bg-slate-100 px-1">host:port:user:pass</code>,
              <code class="rounded bg-slate-100 px-1">user:pass@host:port</code>,
              <code class="rounded bg-slate-100 px-1">socks5://host:port</code>. Строки с <code>#</code> игнорируются.</li>
            <li><b>Import from Provider</b> — тянем прокси из API провайдера (каждый требует свой набор полей).
              В блоке <b>Providers</b> сверху — <b>Re-import</b> (обновить) и <b>Remove all</b> (удалить весь источник).</li>
          </ul>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">Проверка (Check)</h3>
          <p class="mt-1.5 text-slate-600">
            Health-check проверяет соединение и подтягивает <b>external IP, страну, ISP/ASN</b>.
            <b>Check</b> — один прокси, <b>Check All</b> — все (по 5 параллельно). Фильтры сверху: поиск, статус, провайдер.
          </p>
        </section>

        <div class="grid gap-4 sm:grid-cols-2">
          <section>
            <h3 class="font-semibold text-slate-900">Колонки</h3>
            <ul class="mt-1.5 space-y-1 text-slate-600">
              <li><b>Server</b> — <code>протокол://host:port</code> + логин.</li>
              <li><b>Type</b> — residential / mobile / datacenter / proxy.</li>
              <li><b>External IP · ISP / ASN · Country</b> — определяются при проверке.</li>
              <li><b>Source</b> — откуда добавлен (вручную / провайдер).</li>
              <li><b>Last check</b> — когда последний раз проверяли.</li>
            </ul>
          </section>

          <section>
            <h3 class="font-semibold text-slate-900">Статусы</h3>
            <ul class="mt-1.5 space-y-1 text-slate-600">
              <li><span class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-emerald-700">active</span> — рабочий (прошёл проверку)</li>
              <li><span class="rounded-full bg-red-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-red-700">down</span> — не отвечает / ошибка (см. текст под статусом)</li>
              <li><span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium uppercase text-slate-500">unknown</span> — ещё не проверялся</li>
            </ul>
            <p class="mt-2 text-[12px] text-slate-500">В прогон берутся только <b>active</b> прокси.</p>
          </section>
        </div>
      </div>

      <div class="flex justify-end border-t border-slate-100 px-6 py-4">
        <button type="button" onclick={() => (helpOpen = false)}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Понятно</button>
      </div>
    </div>
  </div>
{/if}

<!-- Manual Add modal -->
{#if addOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (addOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Add proxy (manual)</h2>
      <form onsubmit={submitAdd} class="mt-4 space-y-3">
        <div class="grid grid-cols-3 gap-2">
          <div>
            <label for="ap_proto" class="block text-xs font-medium text-slate-700">Protocol</label>
            <select id="ap_proto" bind:value={m_protocol} class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="http">http</option>
              <option value="https">https</option>
              <option value="socks5">socks5</option>
            </select>
          </div>
          <div class="col-span-2">
            <label for="ap_host" class="block text-xs font-medium text-slate-700">Host *</label>
            <input id="ap_host" type="text" bind:value={m_host} required
                   class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm font-mono" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label for="ap_port" class="block text-xs font-medium text-slate-700">Port *</label>
            <input id="ap_port" type="number" bind:value={m_port} required min="1" max="65535"
                   class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
          <div>
            <label for="ap_country" class="block text-xs font-medium text-slate-700">Country (ISO-2)</label>
            <input id="ap_country" type="text" bind:value={m_country} maxlength="2"
                   class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm uppercase" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label for="ap_user" class="block text-xs font-medium text-slate-700">Username</label>
            <input id="ap_user" type="text" bind:value={m_username}
                   class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm font-mono" />
          </div>
          <div>
            <label for="ap_pass" class="block text-xs font-medium text-slate-700">Password</label>
            <input id="ap_pass" type="password" bind:value={m_password}
                   class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
        </div>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label for="ap_type" class="block text-xs font-medium text-slate-700">Type (optional)</label>
            <select id="ap_type" bind:value={m_proxy_type}
                    class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">—</option>
              <option value="residential">residential</option>
              <option value="mobile">mobile</option>
              <option value="datacenter">datacenter</option>
              <option value="proxy">proxy</option>
            </select>
          </div>
          <div>
            <label for="ap_prov" class="block text-xs font-medium text-slate-700">Provider (optional)</label>
            <input id="ap_prov" type="text" bind:value={m_provider}
                   class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
        </div>
        <div>
          <label for="ap_note" class="block text-xs font-medium text-slate-700">Note</label>
          <textarea id="ap_note" bind:value={m_note} rows="2"
                    class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"></textarea>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (addOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Add</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Bulk modal -->
{#if bulkOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (bulkOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Bulk add proxies</h2>
      <p class="mt-1 text-xs text-slate-500">
        Один proxy в строке. Поддерживаемые форматы:
        <code class="rounded bg-slate-100 px-1">host:port</code>,
        <code class="rounded bg-slate-100 px-1">host:port:user:pass</code>,
        <code class="rounded bg-slate-100 px-1">user:pass@host:port</code>,
        <code class="rounded bg-slate-100 px-1">http://user:pass@host:port</code>,
        <code class="rounded bg-slate-100 px-1">socks5://host:port</code>.
        Строки с # игнорируются.
      </p>
      <form onsubmit={submitBulk} class="mt-3 space-y-3">
        <textarea bind:value={bulkText} rows="14" required spellcheck="false"
                  placeholder={'# example:\nuser:pass@1.2.3.4:8080\nhttp://1.2.3.4:8080\nproxy.example.com:3128:myuser:mypass'}
                  class="w-full rounded-md border border-slate-300 px-2 py-2 font-mono text-[13px]"
        ></textarea>
        <div class="flex justify-end gap-2">
          <button type="button" onclick={() => (bulkOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" disabled={bulkBusy || !bulkText.trim()}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {bulkBusy ? 'Adding…' : 'Add all'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Import from provider modal -->
{#if importOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (importOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-lg overflow-auto rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Import from provider</h2>
      <p class="mt-1 text-xs text-slate-500">
        Каждый провайдер требует свой набор полей. Поля помечены * — обязательные.
      </p>

      <form onsubmit={submitImport} class="mt-4 space-y-3">
        <div>
          <label for="imp_prov" class="block text-xs font-medium text-slate-700">Provider</label>
          <select id="imp_prov" bind:value={selectedProvider}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
            {#each sources as s}
              <option value={s.name}>{s.display_name}</option>
            {/each}
          </select>
        </div>

        {#if selectedSource}
          {#each selectedSource.fields as f}
            <div>
              <label for={`imp_${f.name}`} class="block text-xs font-medium text-slate-700">{f.label}</label>
              {#if f.type === 'textarea'}
                <textarea id={`imp_${f.name}`} required={f.required}
                          rows="3"
                          value={fieldValue(f)} oninput={(e) => setFieldValue(f, (e.currentTarget as HTMLTextAreaElement).value)}
                          placeholder={f.placeholder ?? ''}
                          class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                ></textarea>
              {:else if f.type === 'select'}
                <select id={`imp_${f.name}`}
                        value={fieldValue(f)} onchange={(e) => setFieldValue(f, (e.currentTarget as HTMLSelectElement).value)}
                        class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                  {#each (f.options ?? []) as opt}
                    <option value={opt}>{opt}</option>
                  {/each}
                </select>
              {:else}
                <input id={`imp_${f.name}`}
                       type={f.type}
                       required={f.required}
                       value={fieldValue(f)} oninput={(e) => setFieldValue(f, (e.currentTarget as HTMLInputElement).value)}
                       placeholder={f.placeholder ?? ''}
                       class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
              {/if}
              {#if f.help}
                <p class="mt-1 text-[11px] text-slate-400">{f.help}</p>
              {/if}
            </div>
          {/each}
        {/if}

        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (importOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" disabled={importBusy || !selectedSource}
                  class="rounded-md bg-purple-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-purple-700 disabled:bg-slate-300">
            {importBusy ? 'Importing…' : 'Import'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}
