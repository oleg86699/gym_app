<script lang="ts">
  import { Check } from 'lucide-svelte'

  // Общий пикер доступа по тегам батчей (для страниц юзера и групп).
  // Формат — чекбокс-список (как выбор батчей в модалке поставщика), тег в строке.
  let {
    availableTags = [],
    restricted = $bindable(false),
    selected = $bindable([]),
  }: {
    availableTags: string[]
    restricted: boolean
    selected: string[]
  } = $props()

  let search = $state('')
  let filtered = $derived.by(() => {
    const q = search.trim().toLowerCase()
    return q ? availableTags.filter((t) => t.toLowerCase().includes(q)) : availableTags
  })
  function toggle(tag: string) {
    selected = selected.includes(tag)
      ? selected.filter((t) => t !== tag)
      : [...selected, tag]
  }
</script>

<div class="mt-3 flex items-center gap-2">
  <button type="button" onclick={() => (restricted = false)}
          class="rounded-full border px-3 py-1 text-xs font-medium {!restricted ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">
    Все теги
  </button>
  <button type="button" onclick={() => (restricted = true)}
          class="rounded-full border px-3 py-1 text-xs font-medium {restricted ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-300 text-slate-600 hover:bg-slate-50'}">
    Только выбранные
  </button>
  {#if !restricted}<span class="text-[11px] text-slate-400">сейчас: все теги</span>{/if}
</div>

{#if restricted}
  {#if availableTags.length === 0}
    <p class="mt-3 text-[11px] text-slate-400">Тегов пока нет — добавь теги батчам.</p>
  {:else}
    {#if availableTags.length > 8}
      <input bind:value={search} placeholder={`поиск среди ${availableTags.length} тегов…`}
             class="mt-2 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
    {/if}
    <div class="mt-2 max-h-52 space-y-1 overflow-auto rounded-md border border-slate-200 p-1.5">
      {#each filtered as tag}
        {@const sel = selected.includes(tag)}
        <button type="button" onclick={() => toggle(tag)}
                class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left {sel ? 'bg-brand-50 ring-1 ring-brand-300' : 'hover:bg-slate-50'}">
          <span class="flex h-4 w-4 shrink-0 items-center justify-center rounded border {sel ? 'border-brand-500 bg-brand-500 text-white' : 'border-slate-300'}">
            {#if sel}<Check size={11} />{/if}
          </span>
          <span class="min-w-0 flex-1 truncate text-sm font-medium text-slate-800">{tag}</span>
        </button>
      {/each}
      {#if filtered.length === 0}
        <p class="px-2 py-1 text-[11px] text-slate-400">Ничего не найдено.</p>
      {/if}
    </div>
    {#if selected.length === 0}
      <p class="mt-2 text-[11px] text-amber-600">⚠ Пустой список = нет доступа ни к одному тегу.</p>
    {:else}
      <p class="mt-1 text-[11px] text-slate-400">
        Выбрано: <b>{selected.length}</b> тег(ов).
        <button type="button" onclick={() => (selected = [])}
                class="ml-1 underline text-slate-400 hover:text-slate-600">сбросить</button>
      </p>
    {/if}
  {/if}
{/if}
