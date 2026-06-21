<script lang="ts">
  import '../app.css'
  import Toaster from '$lib/components/ui/Toaster.svelte'

  let { children } = $props()
</script>

<svelte:head>
  <title>gym_app</title>
</svelte:head>

{@render children()}
<Toaster />
