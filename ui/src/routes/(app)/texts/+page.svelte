<script lang="ts">
  import { onMount } from 'svelte'
  import { Download, Pencil, Search } from 'lucide-svelte'

  import { texts as textsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { TextSearchRow } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let q = $state('')
  let lang = $state('')
  let reuseOnly = $state(false)
  let rows = $state<TextSearchRow[]>([])
  let loading = $state(false)
  let timer: ReturnType<typeof setTimeout> | null = null

  async function run() {
    loading = true
    try {
      rows = await textsApi.search({
        q: q.trim() || undefined, lang: lang.trim() || undefined,
        reusable_only: reuseOnly, limit: 100,
      })
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  // debounce-поиск при вводе
  function onInput() {
    if (timer) clearTimeout(timer)
    timer = setTimeout(run, 300)
  }
  function toggleReuse() {
    reuseOnly = !reuseOnly
    run()
  }

  onMount(run)

  function fmtDate(s: string): string {
    return new Date(s).toLocaleDateString()
  }
  function host(u: string | null): string {
    if (!u) return ''
    try { return new URL(u).hostname } catch { return u }
  }
  const SOURCE_CLASS: Record<string, string> = {
    human: 'bg-slate-100 text-slate-600',
    generated: 'bg-violet-100 text-violet-700',
    spin_variant: 'bg-amber-100 text-amber-700',
    reused: 'bg-cyan-100 text-cyan-700',
  }

  // ─── Сортировка по колонкам (клиентская, по загруженным результатам) ──
  let sortKey = $state<string | null>(null)
  let sortDir = $state<'asc' | 'desc'>('asc')

  function toggleSort(key: string) {
    if (sortKey === key) sortDir = sortDir === 'asc' ? 'desc' : 'asc'
    else { sortKey = key; sortDir = 'asc' }
  }
  function sortVal(t: TextSearchRow, key: string): string | number {
    switch (key) {
      case 'id': return t.id
      case 'title': return (t.title ?? '').toLowerCase()
      case 'anchor': return (t.anchor ?? '').toLowerCase()
      case 'link': return (t.link ?? '').toLowerCase()
      case 'spin': return t.spin_count
      case 'posted': return t.posted_count
      case 'created': return new Date(t.created_at).getTime()
      default: return 0
    }
  }
  let sortedRows = $derived.by(() => {
    if (!sortKey) return rows
    const k = sortKey, dir = sortDir === 'asc' ? 1 : -1
    return [...rows].sort((a, b) => {
      const va = sortVal(a, k), vb = sortVal(b, k)
      return va < vb ? -dir : va > vb ? dir : 0
    })
  })

  // Ссылки экспорта (cookie-auth, браузер качает) — реактивны к текущим фильтрам
  let exportQuery = $derived({
    q: q.trim() || undefined, lang: lang.trim() || undefined, reusable_only: reuseOnly,
  })
</script>

<div class="space-y-4">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Библиотека текстов</h1>
    <p class="mt-1 text-sm text-slate-500">
      Единое хранилище всех текстов (ручные и сгенерированные). Поиск по ключу/теме
      (полнотекст) + нечётко по заголовку. Основа для reuse.
    </p>
  </div>

  <div class="flex flex-wrap items-center gap-2">
    <div class="relative min-w-[260px] flex-1">
      <Search size={15} class="pointer-events-none absolute left-3 top-2.5 text-slate-400" />
      <input type="text" bind:value={q} oninput={onInput}
             placeholder="Ключевые слова / тема (напр. «футбол ставки»)…"
             class="w-full rounded-md border border-slate-300 py-2 pl-9 pr-3 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
    </div>
    <input type="text" bind:value={lang} oninput={onInput} maxlength="10"
           placeholder="lang (en/ru/uk…)"
           class="w-28 rounded-md border border-slate-300 px-3 py-2 text-sm" />
    <button type="button" onclick={toggleReuse}
            title="Показывать только reusable-оригиналы (годятся для reuse-кампаний)"
            class="rounded-md border px-3 py-2 text-sm font-medium transition"
            class:border-cyan-400={reuseOnly} class:bg-cyan-50={reuseOnly} class:text-cyan-700={reuseOnly}
            class:border-slate-300={!reuseOnly} class:text-slate-600={!reuseOnly} class:hover:bg-slate-50={!reuseOnly}>
      Только reuse
    </button>
    <button type="button" onclick={run}
            class="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700">
      Найти
    </button>
    <div class="ml-auto flex items-center gap-2">
      <a href={textsApi.exportUrl(exportQuery)} download
         class="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
         title="Скачать список (метаданные) в CSV">
        <Download size={14} /> CSV
      </a>
      <a href={textsApi.exportUrl({ ...exportQuery, with_body: true })} download
         class="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50"
         title="Скачать с полными телами текстов (бэкап/перенос)">
        <Download size={14} /> CSV + тексты
      </a>
    </div>
  </div>

  <div class="rounded-lg border border-slate-200 bg-white">
    <div class="flex items-center justify-between border-b border-slate-100 px-4 py-2 text-xs text-slate-500">
      <span>Найдено: {rows.length}{#if reuseOnly} · только reuse{/if}</span>
      {#if loading}<span>Поиск…</span>{/if}
    </div>
    {#if rows.length === 0 && !loading}
      <div class="px-4 py-10 text-center text-sm text-slate-400">
        {q || reuseOnly ? 'Ничего не найдено.' : 'Библиотека пуста — залей тексты через прогон.'}
      </div>
    {:else}
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
      <div class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
            <tr>
              {@render sortHead('id', 'ID')}
              {@render sortHead('title', 'Заголовок / текст', 'min-w-[30rem]')}
              {@render sortHead('anchor', 'Анкор / тема')}
              {@render sortHead('link', 'Ссылка')}
              {@render sortHead('spin', 'Спин', 'text-center')}
              {@render sortHead('posted', 'Постинг', 'text-center')}
              {@render sortHead('created', 'Создан')}
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            {#each sortedRows as t (t.id)}
              <tr class="align-top hover:bg-slate-50">
                <td class="px-3 py-2 text-slate-400">{t.id}</td>
                <!-- Заголовок + текст в одной колонке — клик ведёт в редактор (как в runs) -->
                <td class="px-3 py-2">
                  <a href={`/texts/${t.id}`} class="group block max-w-2xl">
                    <div class="font-medium text-slate-900 group-hover:text-brand-600">{t.title || `Без заголовка #${t.id}`}</div>
                    <p class="mt-0.5 line-clamp-2 text-xs text-slate-500 group-hover:text-brand-600">{t.snippet || '— пусто —'}</p>
                    <span class="mt-0.5 inline-flex items-center gap-1 text-[11px] text-slate-400 group-hover:text-brand-600"><Pencil size={11} /> редактор</span>
                  </a>
                  <div class="mt-1 flex flex-wrap items-center gap-1">
                    <span class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase {SOURCE_CLASS[t.source] ?? 'bg-slate-100 text-slate-600'}">{t.source}</span>
                    {#if t.lang}<span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">{t.lang}</span>{/if}
                    {#if t.reusable}<span class="rounded bg-cyan-100 px-1.5 py-0.5 text-[10px] font-medium text-cyan-700" title="Reusable-оригинал: годится для reuse-кампаний">reuse</span>{/if}
                  </div>
                </td>
                <!-- Анкор / тема -->
                <td class="px-3 py-2">
                  <div class="text-slate-700">{t.anchor || '—'}</div>
                  {#if t.keyword}<div class="mt-0.5 text-[11px] text-slate-400">тема: {t.keyword}</div>{/if}
                </td>
                <!-- Ссылка -->
                <td class="px-3 py-2">
                  {#if t.link}<span class="font-mono text-[12px] text-slate-600">{host(t.link)}</span>{:else}<span class="text-slate-400">—</span>{/if}
                </td>
                <!-- Спин -->
                <td class="px-3 py-2 text-center text-xs">
                  {#if t.spin_count > 0}<span class="font-semibold text-amber-700" title="Спин-вариантов из этого оригинала">{t.spin_count}</span>
                  {:else if t.has_spin}<span class="text-amber-600" title="Есть spintax-формула">spintax</span>
                  {:else}<span class="text-slate-300">—</span>{/if}
                </td>
                <!-- Постинг -->
                <td class="px-3 py-2 text-center text-xs" title="Реально опубликовано постов с этим текстом">
                  <span class="font-semibold {t.posted_count > 0 ? 'text-emerald-600' : 'text-slate-400'}">{t.posted_count}×</span>
                  {#if t.times_used > 0 && t.times_used !== t.posted_count}<div class="text-[10px] text-slate-400">исп. {t.times_used}×</div>{/if}
                </td>
                <!-- Создан -->
                <td class="px-3 py-2 text-xs text-slate-500">{fmtDate(t.created_at)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
</div>
