<script lang="ts">
  import { AlertTriangle, ArrowLeft } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import { postings as postingsApi, projects as projectsApi, textItems as textItemsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import TipTapEditor from '$lib/components/ui/TipTapEditor.svelte'
  import type { TextItemDetail, TextItemStatus } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'

  let runId = $derived(Number(page.params.id))
  let textId = $derived(Number(page.params.textId))

  let item = $state<TextItemDetail | null>(null)
  let loading = $state(true)
  let saving = $state(false)

  // Режимы редактирования — "Visual / HTML" как в WP Classic
  let mode = $state<'visual' | 'html'>('visual')

  let formTitle = $state('')
  let formContent = $state('')

  let dirty = $derived(
    item !== null &&
      (formTitle !== (item.title ?? '') || formContent !== item.content),
  )

  async function load() {
    try {
      item = await textItemsApi.get(textId)
      formTitle = item.title ?? ''
      formContent = item.content
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  onMount(load)

  async function save() {
    if (!item || !item.editable || !dirty) return
    saving = true
    try {
      item = await textItemsApi.update(textId, {
        title: formTitle || null,
        content: formContent,
      })
      formTitle = item.title ?? ''
      formContent = item.content
      showToast('success', 'Saved')
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      saving = false
    }
  }

  function revert() {
    if (!item) return
    formTitle = item.title ?? ''
    formContent = item.content
  }

  // ─── Дозаполнение needs_review-задачи (целевая ссылка + анкор) ─────
  let resolveLink = $state('')
  let resolveAnchor = $state('')
  let resolveBusy = $state(false)

  function pickCandidate(link: string, anchor: string) {
    resolveLink = link
    resolveAnchor = anchor
  }

  async function submitResolve() {
    if (!item || resolveBusy || !resolveLink.trim()) return
    resolveBusy = true
    try {
      const res = await textItemsApi.resolve(textId, {
        link: resolveLink.trim(), anchor: resolveAnchor.trim(),
      })
      showToast('success', `Готово: цель ${res.target_domain}, задача → в очередь`)
      await load()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { resolveBusy = false }
  }

  // ─── Массовое до-заполнение по домену + добавление домена в проект ──
  function domainOf(url: string): string {
    try {
      return new URL(url.trim()).hostname.toLowerCase().replace(/^www\./, '')
    } catch {
      return ''
    }
  }
  // Домен для массовых действий: из выбранной ссылки, а если её ещё не выбрали —
  // из первого задетекченного кандидата (чтобы кнопки были видны сразу).
  let bulkDomain = $derived(
    domainOf(resolveLink) || domainOf(item?.link_candidates?.[0]?.link ?? ''),
  )
  let bulkBusy = $state(false)
  let addDomainBusy = $state(false)

  // Привязать выбранный домен ко ВСЕМ needs_review этого прогона (каждой своя
  // ссылка), без добавления домена в проект.
  async function bulkResolve() {
    if (!item || !bulkDomain || bulkBusy) return
    bulkBusy = true
    try {
      const res = await postingsApi.resolveBulk(item.posting_run_id, bulkDomain)
      showToast('success', `Привязано ${res.resolved}, пропущено ${res.skipped} → в очередь`)
      await load()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { bulkBusy = false }
  }

  // Добавить домен в проект → авто-резолв всех needs_review с ним (и будущих).
  async function addDomainToProject() {
    if (!item || !item.project_id || !bulkDomain || addDomainBusy) return
    addDomainBusy = true
    try {
      const res = await projectsApi.addDomain(item.project_id, bulkDomain)
      showToast('success', `Домен ${res.domain} в проекте — авто-резолв (${res.auto_resolved_runs} прогон.)`)
      await load()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { addDomainBusy = false }
  }

  // Операции над уже опубликованным постом на живом сайте
  let remoteBusy = $state<'update' | 'delete' | null>(null)

  async function pushUpdate() {
    if (!item || remoteBusy) return
    remoteBusy = 'update'
    try {
      // сначала сохраняем правки локально (в нашу БД), затем пушим их на сайт —
      // так редактор = БД = сайт всегда совпадают
      if (dirty) await save()
      const res = await textItemsApi.updateRemote(textId)
      showToast('success', `Пост обновлён на сайте (${res.via}) и синхронизирован локально`)
      await load()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { remoteBusy = null }
  }

  async function pushDelete() {
    if (!item || remoteBusy) return
    if (!confirm('Удалить опубликованный пост с сайта? Действие необратимо на стороне WP.')) return
    remoteBusy = 'delete'
    try {
      const res = await textItemsApi.deleteRemote(textId)
      showToast('success', `Пост удалён с сайта (${res.via})`)
      await load()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { remoteBusy = null }
  }

  function fmtBytes(n: number): string {
    if (n < 1024) return `${n} B`
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
    return `${(n / 1024 / 1024).toFixed(2)} MB`
  }

  function statusClass(s: TextItemStatus): string {
    switch (s) {
      case 'pending':
        return 'bg-slate-100 text-slate-600'
      case 'posting':
        return 'bg-brand-100 text-brand-700'
      case 'posted':
        return 'bg-emerald-100 text-emerald-700'
      case 'failed':
        return 'bg-red-100 text-red-700'
      case 'skipped':
        return 'bg-amber-100 text-amber-700'
      default:
        return 'bg-slate-100 text-slate-500'
    }
  }

  function onKey(e: KeyboardEvent) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      if (item?.editable) save()
    }
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="space-y-4">
  <div>
    <a href={`/runs/${runId}`} class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Run #{runId}</a>
    {#if loading}
      <h1 class="mt-1 text-2xl font-semibold text-slate-900">Loading…</h1>
    {:else if item}
      <div class="mt-1 flex flex-wrap items-center gap-3">
        <h1 class="text-2xl font-semibold text-slate-900">
          {item.title || item.original_filename}
        </h1>
        <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusClass(item.status as TextItemStatus)}">
          {item.status}
        </span>
        {#if !item.editable}
          <span class="rounded-full bg-slate-200 px-2 py-0.5 text-[11px] font-medium uppercase text-slate-700">
            read-only
          </span>
        {/if}
      </div>
      <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>File: <code class="rounded bg-slate-100 px-1">{item.original_filename}</code></span>
        <span>Size: {fmtBytes(item.byte_size)}</span>
        <span>Attempts: {item.attempts}</span>
        {#if item.site}<span>Site: {item.site.domain}</span>{/if}
        {#if item.credential}<span>Credential: {item.credential.login}</span>{/if}
        {#if item.posted_url}
          <span>
            Posted: <a class="text-brand-600 hover:underline" href={item.posted_url} target="_blank" rel="noopener noreferrer">{item.posted_url}</a>
          </span>
        {/if}
      </div>
      {#if item.last_error}
        <div class="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {item.last_error}
        </div>
      {/if}
      {#if item.editable && item.status === 'posted' && item.post_id}
        <div class="mt-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          <AlertTriangle size={14} class="inline-block align-text-bottom" /> Текст опубликован.
          <b>Save</b> сохраняет правки в нашу БД; чтобы залить их в
          {#if item.posted_url}<a class="underline" href={item.posted_url} target="_blank" rel="noopener noreferrer">живой пост</a>{:else}живой пост{/if}
          — нажми <b>«Обновить на сайте»</b> (он сам сохранит локально, чтобы версии совпадали).
        </div>
      {:else if item.editable && item.status === 'skipped'}
        <div class="mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <AlertTriangle size={14} class="inline-block align-text-bottom" /> Текст пропущен (не опубликован). Правки сохранятся локально.
        </div>
      {/if}

      <!-- needs_review: дозаполнить целевую ссылку -->
      {#if item.status === 'needs_review'}
        <div class="mt-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-3 text-xs text-amber-900">
          <div class="font-semibold">
            <AlertTriangle size={14} class="inline-block align-text-bottom" />
            Нужны данные — не определена целевая ссылка
          </div>
          <p class="mt-1 text-amber-800">
            В тексте не нашлось ссылки на домен проекта (или их несколько разных).
            Укажи целевую ссылку и анкор — задача уйдёт в постинг. Можно добавить
            недостающий домен на странице проекта (тогда такие задачи резолвятся автоматически).
          </p>
          {#if item.link_candidates && item.link_candidates.length}
            <div class="mt-2">
              <span class="text-[11px] uppercase tracking-wide text-amber-700">Найдены в тексте:</span>
              <div class="mt-1 flex flex-col gap-1">
                {#each item.link_candidates as c}
                  <button type="button" onclick={() => pickCandidate(c.link, c.anchor)}
                          class="flex items-center gap-2 rounded border border-amber-200 bg-white px-2 py-1 text-left hover:bg-amber-100">
                    {#if c.is_project_domain}<span class="rounded bg-emerald-100 px-1 text-[10px] text-emerald-700">наш</span>{/if}
                    <span class="font-mono text-[11px] text-slate-700">{c.domain ?? '—'}</span>
                    <span class="truncate text-slate-500">{c.anchor || '(без анкора)'}</span>
                  </button>
                {/each}
              </div>
            </div>
          {/if}
          <div class="mt-2 flex flex-wrap items-end gap-2">
            <div>
              <label for="rs_link" class="block text-[10px] uppercase tracking-wide text-amber-700">Целевая ссылка</label>
              <input id="rs_link" type="text" bind:value={resolveLink} placeholder="https://nawal.mx/"
                     class="mt-0.5 w-72 rounded border border-amber-300 px-2 py-1 text-xs" />
            </div>
            <div>
              <label for="rs_anchor" class="block text-[10px] uppercase tracking-wide text-amber-700">Анкор</label>
              <input id="rs_anchor" type="text" bind:value={resolveAnchor} placeholder="Nawal"
                     class="mt-0.5 w-48 rounded border border-amber-300 px-2 py-1 text-xs" />
            </div>
            <button type="button" onclick={submitResolve} disabled={resolveBusy || !resolveLink.trim()}
                    class="rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 disabled:bg-slate-300">
              {resolveBusy ? '…' : 'Дозаполнить → в очередь'}
            </button>
          </div>
          {#if bulkDomain}
            <div class="mt-2 flex flex-wrap items-center gap-2 border-t border-amber-200 pt-2">
              <span class="text-[11px] text-amber-700">Применить ко всем needs_review прогона:</span>
              <button type="button" onclick={bulkResolve} disabled={bulkBusy}
                      class="rounded-md border border-amber-400 bg-white px-3 py-1.5 text-xs font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-50">
                {bulkBusy ? '…' : `Привязать ${bulkDomain} ко всем`}
              </button>
              <button type="button" onclick={addDomainToProject} disabled={addDomainBusy || !item.project_id}
                      class="rounded-md bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700 disabled:bg-slate-300">
                {addDomainBusy ? '…' : `Добавить ${bulkDomain} в проект`}
              </button>
            </div>
          {/if}
        </div>
      {/if}
      {#if item.target_domain || item.lang}
        <div class="mt-2 flex flex-wrap gap-3 text-[11px] text-slate-500">
          {#if item.target_domain}<span>Цель: <code class="rounded bg-slate-100 px-1">{item.target_domain}</code></span>{/if}
          {#if item.lang}<span>Язык: <code class="rounded bg-slate-100 px-1">{item.lang}</code></span>{/if}
        </div>
      {/if}
    {/if}
  </div>

  {#if !loading && item}
    <!-- Toolbar -->
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div class="inline-flex overflow-hidden rounded-md border border-slate-300 text-xs font-medium">
        {#each ['visual', 'html'] as m}
          {@const isOn = mode === m}
          <button type="button" onclick={() => (mode = m as typeof mode)}
                  class="px-3 py-1.5 transition"
                  class:bg-brand-600={isOn}
                  class:text-white={isOn}
                  class:bg-white={!isOn}
                  class:text-slate-700={!isOn}
                  class:hover:bg-slate-50={!isOn}>
            {m === 'visual' ? 'Visual' : 'HTML'}
          </button>
        {/each}
      </div>
      {#if item.editable}
        <div class="flex flex-wrap items-center gap-2">
          {#if dirty}
            <button type="button" onclick={revert} disabled={saving}
                    title="Откатить несохранённые правки заголовка и текста к последнему сохранённому состоянию (на сайте ничего не меняется)"
                    class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              Сбросить правки
            </button>
          {/if}
          <button type="button" onclick={save} disabled={!dirty || saving}
                  title="Сохранить правки в нашу БД (опубликованный пост не обновляется — для этого «Обновить на сайте»)"
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {saving ? 'Saving…' : 'Save'}
          </button>
          {#if item.status === 'posted' && item.post_id}
            <button type="button" onclick={pushUpdate} disabled={remoteBusy !== null}
                    title="Перезалить текущий текст в уже опубликованный пост на сайте (через рабочий доступ к домену; если их несколько — перебирает, пока не выйдет)"
                    class="rounded-md border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-sm font-medium text-emerald-800 hover:bg-emerald-100 disabled:opacity-50">
              {remoteBusy === 'update' ? 'Обновляю…' : 'Обновить на сайте'}
            </button>
            <button type="button" onclick={pushDelete} disabled={remoteBusy !== null}
                    title="Удалить опубликованный пост с сайта (через рабочий доступ к домену, с перебором)"
                    class="rounded-md border border-red-300 bg-red-50 px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-100 disabled:opacity-50">
              {remoteBusy === 'delete' ? 'Удаляю…' : 'Удалить с сайта'}
            </button>
          {/if}
        </div>
      {/if}
    </div>

    <!-- Title -->
    <div>
      <label for="ti_title" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Title</label>
      <input id="ti_title" type="text" bind:value={formTitle}
             readonly={!item.editable}
             class="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 read-only:bg-slate-50 read-only:text-slate-500" />
      <p class="mt-1 text-[11px] text-slate-400">Заголовок будущего поста (WP <code class="rounded bg-slate-100 px-1">post_title</code>).</p>
    </div>

    <!-- Editor: Visual (TipTap) or HTML source -->
    {#if mode === 'visual'}
      <TipTapEditor bind:content={formContent} readonly={!item.editable} />
    {:else}
      <div class="flex flex-col">
        <div class="mb-1 flex items-center justify-between text-xs font-medium uppercase tracking-wider text-slate-500">
          <span>HTML source</span>
          <span class="font-mono normal-case text-slate-400">{formContent.length} chars</span>
        </div>
        <textarea bind:value={formContent} readonly={!item.editable} spellcheck="false"
                  class="min-h-[60vh] w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-[13px] leading-snug text-slate-800 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 read-only:bg-slate-50"
        ></textarea>
      </div>
    {/if}

    {#if item.editable}
      <p class="text-xs text-slate-400">
        Cmd/Ctrl+S — save.
        {#if item.status === 'pending' || item.status === 'failed'}
          Изменения уйдут при ближайшем посте.
        {/if}
      </p>
    {/if}
  {/if}
</div>
