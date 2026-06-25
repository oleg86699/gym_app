<script lang="ts">
  import { ArrowLeft, ArrowRight } from 'lucide-svelte'
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { onDestroy, onMount } from 'svelte'

  import { postings as postingsApi, projects as projectsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import { runModeLabel } from '$lib/runLabels'
  import type {
    DomainAnalyticsRow,
    PostingRun, PostingRunPriority, PostingRunStatus, Project, ProjectDomain,
  } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let projectId = $derived(Number(page.params.id))

  let project = $state<Project | null>(null)
  let runs = $state<PostingRun[]>([])
  let loading = $state(true)

  // ─── Целевые домены проекта (Фаза A) ───────────────────────────────
  let domains = $state<ProjectDomain[]>([])
  let domainAnalytics = $state<DomainAnalyticsRow[]>([])
  let newDomain = $state('')
  let domainBusy = $state(false)

  async function loadDomains() {
    try {
      domains = await projectsApi.listDomains(projectId)
      domainAnalytics = await projectsApi.domainAnalytics(projectId)
    } catch { /* нет доступа/проекта — молча */ }
  }
  async function addDomain() {
    // парсим список: по строкам / через запятую / пробел / точку с запятой
    const list = newDomain.split(/[\n,;\s]+/).map((s) => s.trim()).filter(Boolean)
    if (list.length === 0 || domainBusy) return
    domainBusy = true
    try {
      const res = await projectsApi.addDomains(projectId, list)
      newDomain = ''
      const parts = [`добавлено: ${res.added.length}`]
      if (res.duplicates.length) parts.push(`дубликатов: ${res.duplicates.length}`)
      if (res.invalid.length) parts.push(`невалидных: ${res.invalid.length}`)
      if (res.auto_resolved_runs) parts.push(`авто-резолв в ${res.auto_resolved_runs} прогон(ах)`)
      showToast(res.added.length ? 'success' : 'info', parts.join(' · '))
      if (res.invalid.length) showToast('warning', `Не распознаны: ${res.invalid.join(', ')}`)
      await loadDomains()
      if (res.auto_resolved_runs) await loadRuns()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally { domainBusy = false }
  }
  async function removeDomain(id: number) {
    try {
      await projectsApi.removeDomain(projectId, id)
      await loadDomains()
    } catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }


  // Create run modal (post + link-режимы)
  let createOpen = $state(false)
  let createBusy = $state(false)
  let newTaskType = $state<'post' | 'sitewide_link' | 'homepage_link'>('post')
  let newInputFormat = $state<'archive' | 'csv' | 'campaign'>('archive')  // post: .zip / csv-direct / кампания
  let newName = $state('')
  let newFile = $state<File | null>(null)
  let newPriority = $state<PostingRunPriority>('normal')
  let newScheduledFor = $state('')  // datetime-local string
  let newSpreadDays = $state(0)  // drip-feed: размазать постинг на N дней (0 = сразу)
  let linkRows = $state<{ url: string; anchor: string }[]>([{ url: '', anchor: '' }])
  let linkSites = $state(10)  // на сколько сайтов ставить каждую ссылку (count)
  let linkCandidates = $state<number | null>(null)

  async function refreshLinkCandidates() {
    if (newTaskType === 'post') { linkCandidates = null; return }
    try {
      linkCandidates = (await postingsApi.linkCandidates(projectId)).candidates
    } catch { linkCandidates = null }
  }

  async function selectTaskType(t: 'post' | 'sitewide_link' | 'homepage_link') {
    newTaskType = t
    if (t !== 'post') {
      if (newName.startsWith('Run ')) newName = `Links ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`
      await refreshLinkCandidates()
    }
  }

  let pollTimer: ReturnType<typeof setInterval> | null = null

  function canManage(p: Project | null): boolean {
    const u = $currentUser
    if (!u || !p) return false
    if (u.is_super_admin) return true
    if (p.owner.id === u.id) return true
    if (u.roles.some((r) => r.name === 'group_admin') && p.owner_group?.id === u.group?.id) return true
    return false
  }

  async function loadProject() {
    try {
      project = await projectsApi.get(projectId)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function loadRuns() {
    try {
      runs = await postingsApi.listForProject(projectId)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function refresh() {
    loading = true
    await Promise.all([loadProject(), loadRuns()])
    loading = false
  }

  function hasActiveRuns(): boolean {
    const active = new Set(['unpacking', 'scheduled', 'queued', 'running', 'paused'])
    return runs.some((r) => active.has(r.status))
  }

  function tickPoll() {
    if (hasActiveRuns()) {
      loadRuns()
    }
  }

  onMount(async () => {
    await refresh()
    await loadDomains()
    // poll каждые 5 сек пока есть активные прогоны
    pollTimer = setInterval(tickPoll, 5000)
  })

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer)
  })

  function openCreate() {
    // Единая (новая) форма создания run'а живёт на /runs — ведём туда с
    // предвыбранным проектом, чтобы не дублировать форму на странице проекта.
    goto(`/runs?new=${projectId}`)
  }

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    createBusy = true
    try {
      if (newTaskType === 'post') {
        if (!newFile) { showToast('error', 'Выбери файл'); createBusy = false; return }
        const params = {
          name: newName,
          priority: newPriority,
          scheduled_for: newScheduledFor ? new Date(newScheduledFor).toISOString() : null,
          spread_days: newSpreadDays || 0,
        }
        const run = newInputFormat === 'csv'
          ? await postingsApi.createCsvDirect(projectId, newFile, params)
          : newInputFormat === 'campaign'
            ? await postingsApi.createCampaign(projectId, newFile, params)
            : await postingsApi.create(projectId, newFile, params)
        showToast('success', `Run "${run.name}" created`)
      } else {
        const links = linkRows
          .map((r) => ({ url: r.url.trim(), anchor: r.anchor.trim() }))
          .filter((r) => r.url)
        if (links.length === 0) { showToast('error', 'Добавь хотя бы одну ссылку (URL)'); createBusy = false; return }
        // строим CSV anchor,link,count (count = на сколько сайтов) и шлём файлом
        const cnt = Math.max(1, linkSites)
        const csv = 'anchor,link,count\n' + links.map((l) =>
          `"${l.anchor.replace(/"/g, '""')}","${l.url.replace(/"/g, '""')}",${cnt}`).join('\n')
        const file = new File([csv], 'links.csv', { type: 'text/csv' })
        const run = await postingsApi.createLinkRun(projectId, file, {
          name: newName, task_type: newTaskType, priority: newPriority,
        })
        showToast('success', `Link-run "${run.name}" создан (${run.total_texts} целей). Запусти кнопкой Start.`)
      }
      createOpen = false
      await loadRuns()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      createBusy = false
    }
  }

  function statusBadgeClass(s: PostingRunStatus): string {
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

  function progressPct(r: PostingRun): number {
    if (r.total_texts === 0) return 0
    return Math.min(100, Math.round(((r.posted_count + r.failed_count + r.skipped_count) / r.total_texts) * 100))
  }
</script>

<div class="space-y-6">
  <div>
    <a href="/projects" class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Projects</a>
    <h1 class="mt-1 text-2xl font-semibold text-slate-900">
      {project?.name ?? '…'}
    </h1>
    {#if project?.description}
      <p class="mt-1 text-sm text-slate-500">{project.description}</p>
    {/if}
    {#if project}
      <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span>Owner: <strong>@{project.owner.username}</strong></span>
        {#if project.owner_group}
          <span>· Group: <strong>#{project.owner_group.name}</strong></span>
        {/if}
      </div>

      <!-- Целевые домены проекта (money-домены, на которые ставим ссылки) -->
      <div class="mt-3 rounded-md border border-slate-200 bg-white px-3 py-2.5 text-sm">
        <div class="flex items-center gap-2">
          <span class="font-medium text-slate-700">Целевые домены проекта</span>
          <span class="text-[11px] text-slate-400">
            (money-домены, на которые ведут ссылки в текстах; по ним разбираем бэклинки и считаем аналитику)
          </span>
        </div>
        {#if canManage(project)}
          <div class="mt-2 flex items-start gap-2">
            <textarea bind:value={newDomain} rows="3"
                      placeholder={'Список доменов — по одному на строку или через запятую:\nnawal.mx\nhttps://footbal.net.ua/\nexample.com'}
                      class="w-96 rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs"></textarea>
            <button type="button" onclick={addDomain} disabled={domainBusy || !newDomain.trim()}
                    class="rounded-md bg-brand-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
              {domainBusy ? '…' : 'Добавить список'}
            </button>
          </div>
        {/if}
        {#if domains.length === 0}
          <p class="mt-2 text-[11px] text-amber-600">
            Доменов пока нет. Без них из заливаемых текстов не определить целевую ссылку — задачи уйдут в «нужны данные».
          </p>
        {:else}
          <div class="mt-2 flex flex-wrap gap-1.5">
            {#each domains as d (d.id)}
              {@const stat = domainAnalytics.find((a) => a.target_domain === d.domain)}
              <span class="group inline-flex items-center gap-1.5 rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-xs text-indigo-700 transition hover:border-indigo-400 hover:bg-indigo-100">
                <a href={`/projects/${projectId}/domains/${encodeURIComponent(d.domain)}`}
                   class="inline-flex items-center gap-1.5 font-medium hover:underline"
                   title="Открыть страницу домена: задачи и аналитика">
                  {d.domain}
                  {#if stat}<span class="text-[10px] text-indigo-400">✓{stat.posted}/{stat.total}</span>{/if}
                  <span class="text-[10px] text-indigo-400">→</span>
                </a>
                {#if canManage(project)}
                  <button type="button" onclick={() => removeDomain(d.id)}
                          title="Удалить домен" class="text-indigo-300 hover:text-red-600">×</button>
                {/if}
              </span>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else}
    <!-- Runs section -->
    <section>
      <div class="mb-3 flex items-center justify-between">
        <h2 class="text-lg font-medium text-slate-900">Posting runs</h2>
        {#if canManage(project)}
          <button onclick={openCreate}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
            + New run
          </button>
        {/if}
      </div>

      {#if runs.length === 0}
        <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
          No runs yet. {#if canManage(project)}Create one — загрузи .zip с .txt текстами.{/if}
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-4 py-2">ID</th>
                <th class="px-4 py-2">Name</th>
                <th class="px-4 py-2">Status</th>
                <th class="px-4 py-2 text-center">Progress</th>
                <th class="px-4 py-2 text-center">Texts</th>
                <th class="px-4 py-2 text-center">Scheduled</th>
                <th class="px-4 py-2 text-center">Created</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each runs as r (r.id)}
                {@const pct = progressPct(r)}
                <tr class="hover:bg-slate-50">
                  <td class="px-4 py-2 text-slate-500">{r.id}</td>
                  <td class="px-4 py-2 font-medium text-slate-900">
                    <a href={`/runs/${r.id}`} class="hover:text-brand-600 hover:underline">{r.name}</a>
                    <span class="mt-0.5 block text-[11px] font-normal text-indigo-600">{runModeLabel(r)}</span>
                  </td>
                  <td class="px-4 py-2">
                    <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {statusBadgeClass(r.status)}">
                      {r.status.replace('_', ' ')}
                    </span>
                  </td>
                  <td class="px-4 py-2 text-center">
                    {#if r.total_texts > 0}
                      <div class="flex items-center gap-2">
                        <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200">
                          <div class="h-full bg-brand-500 transition-all" style="width: {pct}%"></div>
                        </div>
                        <span class="text-xs text-slate-500 w-8 text-right">{pct}%</span>
                      </div>
                    {:else}
                      <span class="text-xs text-slate-400">—</span>
                    {/if}
                  </td>
                  <td class="px-4 py-2 text-center text-xs text-slate-600">
                    {#if r.total_texts > 0}
                      <span class="text-emerald-700">{r.posted_count}</span>
                      / <span class="text-red-600">{r.failed_count}</span>
                      / <span>{r.total_texts}</span>
                    {:else}
                      —
                    {/if}
                  </td>
                  <td class="px-4 py-2 text-center text-xs text-slate-500">
                    {r.scheduled_for ? new Date(r.scheduled_for).toLocaleString() : '—'}
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
          <p class="mt-2 text-xs text-slate-400">Auto-refresh каждые 5 сек пока есть активные прогоны</p>
        {/if}
      {/if}
    </section>
  {/if}
</div>

<!-- Create run modal -->
{#if createOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-md overflow-auto rounded-lg bg-white p-6 shadow-xl"
         onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">New run</h2>

      <div class="mt-3 flex gap-1">
        {#each [['post', 'Пост'], ['sitewide_link', 'Сквозная'], ['homepage_link', 'С главной']] as [val, label]}
          {@const on = newTaskType === val}
          <button type="button" onclick={() => selectTaskType(val as 'post' | 'sitewide_link' | 'homepage_link')}
                  class="flex-1 rounded-md border px-2 py-1.5 text-xs font-medium transition"
                  class:border-brand-600={on} class:bg-brand-50={on} class:text-brand-700={on}
                  class:border-slate-300={!on} class:text-slate-600={!on} class:hover:bg-slate-50={!on}>
            {label}
          </button>
        {/each}
      </div>
      <p class="mt-2 text-xs text-slate-500">
        {#if newTaskType === 'post'}
          Загрузи .zip с .txt файлами. Каждый файл — один пост (<code class="rounded bg-slate-100 px-1">&lt;title&gt;</code> = заголовок).
        {:else if newTaskType === 'sitewide_link'}
          Сквозная ссылка в footer/header (на всех страницах) на admin-сайтах — виджет/меню/шаблон, с проверкой.
        {:else}
          Ссылка с главной: в контенте статической главной или в FSE-шаблоне главной, с проверкой.
        {/if}
        {#if newTaskType !== 'post' && linkCandidates !== null}
          <span class="font-medium text-blue-700">Доступно admin-сайтов: {linkCandidates}.</span>
        {/if}
      </p>

      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div>
          <label for="nr_name" class="block text-sm font-medium text-slate-700">Name *</label>
          <input id="nr_name" type="text" bind:value={newName} required minlength="1" maxlength="255"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>

        {#if newTaskType === 'post'}
        <div>
          <span class="block text-sm font-medium text-slate-700">Формат входа</span>
          <div class="mt-1 inline-flex flex-wrap overflow-hidden rounded-md border border-slate-300 text-xs font-medium">
            {#each [['archive','Архив .zip'],['csv','CSV: link,anchor,text'],['campaign','Кампания: links,anchor,counts']] as opt}
              {@const on = newInputFormat === opt[0]}
              <button type="button" onclick={() => { newInputFormat = opt[0] as 'archive'|'csv'|'campaign'; newFile = null }}
                      class="px-3 py-1.5 transition" class:bg-brand-600={on} class:text-white={on}
                      class:bg-white={!on} class:text-slate-700={!on}>{opt[1]}</button>
            {/each}
          </div>
        </div>
        <div>
          <label for="nr_file" class="block text-sm font-medium text-slate-700">
            {#if newInputFormat === 'csv'}CSV/XLSX (link, anchor, text) *
            {:else if newInputFormat === 'campaign'}CSV/XLSX (links, anchor, counts) *
            {:else}Archive (.zip) *{/if}
          </label>
          <input id="nr_file" type="file"
                 accept={newInputFormat === 'archive' ? '.zip,application/zip' : '.csv,.xlsx'} required
                 onchange={(e) => { newFile = (e.currentTarget as HTMLInputElement).files?.[0] ?? null }}
                 class="mt-1 w-full text-sm" />
          <p class="mt-1 text-[11px] text-slate-400">
            {#if newInputFormat === 'csv'}
              Столбцы <code class="rounded bg-slate-100 px-1">link, anchor, text</code> — данные заданы напрямую (без распаковки и подбора домена).
            {:else if newInputFormat === 'campaign'}
              Столбцы <code class="rounded bg-slate-100 px-1">links, anchor, counts</code> — на каждую строку <i>counts</i> задач; тексты берём из библиотеки (reuse: чистим старую ссылку, инжектим новую). Нужны тексты в библиотеке.
            {:else}
              .zip с .txt; ссылку/анкор вытащим из текста по доменам проекта.
            {/if}
          </p>
          {#if newFile}
            <p class="mt-1 text-xs text-slate-500">
              {newFile.name} · {(newFile.size / 1024).toFixed(1)} KB
            </p>
          {/if}
        </div>
        {:else}
        <div>
          <span class="block text-sm font-medium text-slate-700">Ссылки (URL · anchor)</span>
          <div class="mt-1 space-y-2">
            {#each linkRows as row, i}
              <div class="flex gap-2">
                <input type="url" placeholder="https://target.com/page" bind:value={row.url}
                       class="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm font-mono" />
                <input type="text" placeholder="anchor text" bind:value={row.anchor}
                       class="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
                <button type="button" title="Убрать"
                        onclick={() => { linkRows = linkRows.filter((_, j) => j !== i) }}
                        class="rounded-md border border-slate-300 px-2 text-slate-400 hover:text-red-600">×</button>
              </div>
            {/each}
          </div>
          <button type="button" onclick={() => { linkRows = [...linkRows, { url: '', anchor: '' }] }}
                  class="mt-2 text-xs font-medium text-brand-600 hover:underline">+ ещё ссылку</button>
          <div class="mt-2 flex items-center gap-2">
            <label for="lnk_sites" class="text-xs font-medium text-slate-700">Сайтов на ссылку</label>
            <input id="lnk_sites" type="number" min="1" max="100000" bind:value={linkSites}
                   class="w-24 rounded-md border border-slate-300 px-2 py-1 text-sm" />
          </div>
          <p class="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] text-amber-700">
            Каждая ссылка ставится на N доступных admin-сайтов (без пересечений). После создания — <b>Start</b>.
          </p>
        </div>
        {/if}

        <div>
          <span class="block text-sm font-medium text-slate-700">Priority</span>
          <div class="mt-1 flex gap-1">
            {#each [['low', 'Low'], ['normal', 'Normal'], ['high', 'High']] as [val, label]}
              {@const isOn = newPriority === val}
              <button type="button"
                      onclick={() => (newPriority = val as PostingRunPriority)}
                      class="flex-1 rounded-md border px-2 py-1.5 text-xs font-medium transition"
                      class:border-brand-600={isOn}
                      class:bg-brand-50={isOn}
                      class:text-brand-700={isOn}
                      class:border-slate-300={!isOn}
                      class:text-slate-600={!isOn}
                      class:hover:bg-slate-50={!isOn}>
                {label}
              </button>
            {/each}
          </div>
          <p class="mt-1 text-[11px] text-slate-400">
            High пойдёт в работу раньше прогонов с Normal/Low, ждущих в очереди.
          </p>
        </div>

        {#if newTaskType === 'post'}
        <div>
          <label for="nr_sched" class="block text-sm font-medium text-slate-700">
            Scheduled start <span class="text-slate-400">(пусто <ArrowRight size={14} class="inline-block align-text-bottom" /> сразу после распаковки)</span>
          </label>
          <input id="nr_sched" type="datetime-local" bind:value={newScheduledFor}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label for="nr_spread" class="block text-sm font-medium text-slate-700">
            Разбить на дней <span class="text-slate-400">(0 <ArrowRight size={14} class="inline-block align-text-bottom" /> постить всё сразу)</span>
          </label>
          <input id="nr_spread" type="number" min="0" max="365" bind:value={newSpreadDays}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <p class="mt-1 text-[11px] text-slate-400">
            Drip-feed: тексты размажутся по окну от старта на N дней. Прогон сам
            «засыпает» между порциями и продолжает по расписанию.
          </p>
        </div>
        {/if}

        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Cancel
          </button>
          <button type="submit" disabled={createBusy || (newTaskType === 'post' && !newFile)}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {createBusy ? 'Creating…' : (newTaskType === 'post' ? 'Create run' : 'Создать link-run')}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

