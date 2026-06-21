<script lang="ts">
  import { ChevronDown } from 'lucide-svelte'
  import { onDestroy } from 'svelte'

  interface MenuItem {
    label: string
    href?: string
    onClick?: () => void
    description?: string
    download?: string
  }

  interface Props {
    label?: string
    items: MenuItem[]
    align?: 'left' | 'right'
    disabled?: boolean
    title?: string
  }

  let {
    label = 'Menu',
    items,
    align = 'right',
    disabled = false,
    title = '',
  }: Props = $props()

  let open = $state(false)
  let containerEl: HTMLDivElement

  function onDocClick(e: MouseEvent) {
    if (!open) return
    if (containerEl && !containerEl.contains(e.target as Node)) {
      open = false
    }
  }

  function onEsc(e: KeyboardEvent) {
    if (e.key === 'Escape') open = false
  }

  $effect(() => {
    if (open) {
      document.addEventListener('click', onDocClick)
      document.addEventListener('keydown', onEsc)
    } else {
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onEsc)
    }
  })

  onDestroy(() => {
    document.removeEventListener('click', onDocClick)
    document.removeEventListener('keydown', onEsc)
  })

  function handleItemClick(item: MenuItem, e: MouseEvent) {
    if (item.onClick) {
      e.preventDefault()
      item.onClick()
    }
    open = false
  }
</script>

<div bind:this={containerEl} class="relative inline-block">
  <button type="button" {disabled} {title}
          onclick={(e) => { e.stopPropagation(); open = !open }}
          class="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-40">
    {label}
    <span class="text-slate-400" aria-hidden="true"><ChevronDown size={14} class="inline-block align-text-bottom" /></span>
  </button>

  {#if open}
    <div
      class="absolute z-20 mt-1 w-56 overflow-hidden rounded-md border border-slate-200 bg-white py-1 shadow-lg"
      class:right-0={align === 'right'}
      class:left-0={align === 'left'}
      role="menu"
    >
      {#each items as item}
        {#if item.href}
          <a href={item.href} download={item.download}
             onclick={(e) => handleItemClick(item, e)}
             class="block px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">
            <div class="font-medium">{item.label}</div>
            {#if item.description}
              <div class="mt-0.5 text-[11px] text-slate-400">{item.description}</div>
            {/if}
          </a>
        {:else}
          <button type="button" onclick={(e) => handleItemClick(item, e)}
                  class="block w-full px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100">
            <div class="font-medium">{item.label}</div>
            {#if item.description}
              <div class="mt-0.5 text-[11px] text-slate-400">{item.description}</div>
            {/if}
          </button>
        {/if}
      {/each}
    </div>
  {/if}
</div>
