<script lang="ts">
  import { ArrowLeft } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import { texts as textsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import TipTapEditor from '$lib/components/ui/TipTapEditor.svelte'
  import HtmlSourceEditor from '$lib/components/ui/HtmlSourceEditor.svelte'
  import type { TextDetail } from '$lib/api/types'
  import { formatHtml } from '$lib/html-format'
  import { showToast } from '$lib/stores/toast'

  let textId = $derived(Number(page.params.id))

  let text = $state<TextDetail | null>(null)
  let loading = $state(true)
  let saving = $state(false)
  let mode = $state<'visual' | 'html'>('visual')

  let formTitle = $state('')
  let formBody = $state('')
  // Буфер HTML-source: pretty-print при переключении в html (только для показа/
  // правки исходника; в formBody уезжает реально набранное).
  let sourceText = $state('')

  function switchMode(m: 'visual' | 'html') {
    if (m === 'html' && mode !== 'html') sourceText = formatHtml(formBody)
    mode = m
  }

  let dirty = $derived(
    text !== null && (formTitle !== (text.title ?? '') || formBody !== text.body),
  )

  async function load() {
    try {
      text = await textsApi.get(textId)
      formTitle = text.title ?? ''
      formBody = text.body
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(load)

  async function save() {
    if (!text || !dirty) return
    saving = true
    try {
      text = await textsApi.update(textId, { title: formTitle || null, body: formBody })
      formTitle = text.title ?? ''
      formBody = text.body
      showToast('success', 'Сохранено')
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      saving = false
    }
  }
  function revert() {
    if (!text) return
    formTitle = text.title ?? ''
    formBody = text.body
  }
  function onKey(e: KeyboardEvent) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      e.preventDefault()
      save()
    }
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="space-y-4">
  <div>
    <a href="/texts" class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Библиотека текстов</a>
    {#if loading}
      <h1 class="mt-1 text-2xl font-semibold text-slate-900">Loading…</h1>
    {:else if text}
      <div class="mt-1 flex flex-wrap items-center gap-3">
        <h1 class="text-2xl font-semibold text-slate-900">{text.title || `Без заголовка #${text.id}`}</h1>
        <span class="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-medium uppercase text-violet-700">{text.source}</span>
        {#if text.lang}<span class="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500">{text.lang}</span>{/if}
        {#if text.reusable}<span class="rounded-full bg-cyan-100 px-2 py-0.5 text-[11px] font-medium text-cyan-700">reuse</span>{/if}
      </div>
      <div class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>ID: <code class="rounded bg-slate-100 px-1">{text.id}</code></span>
        <span>Использован: {text.times_used}×</span>
        {#if text.parent_text_id}<span>Оригинал: <a href={`/texts/${text.parent_text_id}`} class="text-brand-600 hover:underline">#{text.parent_text_id}</a></span>{/if}
        {#if text.spin_formula}<span class="text-amber-600">есть spintax-формула</span>{/if}
      </div>
    {/if}
  </div>

  {#if text}
    <!-- Тулбар -->
    <div class="flex items-center justify-between">
      <div class="inline-flex overflow-hidden rounded-md border border-slate-300 text-xs font-medium">
        {#each ['visual', 'html'] as m}
          {@const isOn = mode === m}
          <button type="button" onclick={() => switchMode(m as typeof mode)}
                  class="px-3 py-1.5 transition" class:bg-brand-600={isOn} class:text-white={isOn}
                  class:bg-white={!isOn} class:text-slate-700={!isOn}>
            {m === 'visual' ? 'Visual' : 'HTML'}
          </button>
        {/each}
      </div>
      <div class="flex items-center gap-2">
        {#if dirty}
          <button type="button" onclick={revert} disabled={saving}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-50">
            Отменить
          </button>
        {/if}
        <button type="button" onclick={save} disabled={!dirty || saving}
                class="rounded-md bg-brand-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
          {saving ? 'Сохранение…' : 'Сохранить'}
        </button>
      </div>
    </div>

    <!-- Title -->
    <div>
      <label for="t_title" class="block text-xs font-medium uppercase tracking-wider text-slate-500">Заголовок</label>
      <input id="t_title" type="text" bind:value={formTitle}
             class="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
    </div>

    <!-- Editor -->
    {#if mode === 'visual'}
      <TipTapEditor bind:content={formBody} />
    {:else}
      <div class="flex flex-col">
        <div class="mb-1 flex items-center justify-between text-xs font-medium uppercase tracking-wider text-slate-500">
          <span>HTML source</span>
          <span class="font-mono normal-case text-slate-400">{sourceText.length} chars</span>
        </div>
        <HtmlSourceEditor bind:value={sourceText} oninput={(v) => (formBody = v)} />
      </div>
    {/if}

    <p class="text-xs text-slate-400">Cmd/Ctrl+S — сохранить. Правки меняют тело в библиотеке (повлияют на будущие reuse/спины).</p>
  {/if}
</div>
