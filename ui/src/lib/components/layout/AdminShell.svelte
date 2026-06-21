<script lang="ts">
  import Sidebar from './Sidebar.svelte'
  import Topbar from './Topbar.svelte'

  let { children } = $props()
</script>

<div class="flex h-full min-h-screen bg-slate-50">
  <Sidebar />
  <div class="flex min-h-screen flex-1 flex-col">
    <Topbar />
    <main class="flex-1 overflow-auto p-6">
      {@render children()}
    </main>
  </div>
</div>
