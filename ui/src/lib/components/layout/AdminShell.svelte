<script lang="ts">
  import { afterNavigate } from '$app/navigation'
  import Sidebar from './Sidebar.svelte'
  import Topbar from './Topbar.svelte'

  let { children } = $props()

  // Скролл теперь внутри <main>, а не на body — поэтому штатный SvelteKit
  // scroll-to-top при переходах его не трогает. Сбрасываем вручную.
  let mainEl = $state<HTMLElement | undefined>()
  afterNavigate(() => mainEl?.scrollTo({ top: 0 }))
</script>

<!--
  Фикс-height оболочка: страница ровно 100vh и не растёт (overflow-hidden),
  скролл — только внутри <main>. `min-h-0` на main обязателен: без него flex-item
  не «сжимается» и overflow-auto не создаёт scroll-контейнер → на невысоких экранах
  / в других браузерах низ страницы обрезался (см. min-h-screen-версию до этого).
-->
<div class="flex h-screen overflow-hidden bg-slate-50">
  <Sidebar />
  <div class="flex min-w-0 flex-1 flex-col">
    <Topbar />
    <main bind:this={mainEl} class="min-h-0 flex-1 overflow-auto p-6">
      {@render children()}
    </main>
  </div>
</div>
