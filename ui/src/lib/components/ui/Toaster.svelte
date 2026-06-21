<script lang="ts">
  import { dismissToast, toasts } from '$lib/stores/toast'
</script>

<div class="pointer-events-none fixed right-4 top-4 z-50 flex w-80 flex-col gap-2">
  {#each $toasts as t (t.id)}
    <div
      class="pointer-events-auto flex items-start gap-3 rounded-md border bg-white p-3 shadow-sm"
      class:border-blue-200={t.kind === 'info'}
      class:border-emerald-200={t.kind === 'success'}
      class:border-amber-200={t.kind === 'warning'}
      class:border-red-200={t.kind === 'error'}
    >
      <div
        class="mt-1 h-2 w-2 shrink-0 rounded-full"
        class:bg-blue-500={t.kind === 'info'}
        class:bg-emerald-500={t.kind === 'success'}
        class:bg-amber-500={t.kind === 'warning'}
        class:bg-red-500={t.kind === 'error'}
      ></div>
      <div class="flex-1 text-sm text-slate-700">{t.message}</div>
      <button
        type="button"
        class="text-slate-400 hover:text-slate-600"
        onclick={() => dismissToast(t.id)}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  {/each}
</div>
