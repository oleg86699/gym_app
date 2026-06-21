<script lang="ts">
  import { page } from '$app/state'
  import AdminShell from '$lib/components/layout/AdminShell.svelte'
  import { currentUser, hasPageAccess } from '$lib/stores/user'

  let { children } = $props()

  // Закрытие страниц по page-access — на стороне UI (бэк дублирует на API).
  let accessDenied = $derived.by(() => {
    const u = $currentUser
    if (!u) return false
    return !hasPageAccess(u, page.url.pathname.replace(/\/$/, '') || '/')
  })
</script>

<AdminShell>
  {#if accessDenied}
    <div class="mx-auto mt-12 max-w-md rounded-lg border border-amber-200 bg-amber-50 p-6 text-center">
      <h2 class="text-lg font-semibold text-amber-800">Access denied</h2>
      <p class="mt-1 text-sm text-amber-700">
        You don't have access to this page. Contact super_admin to request access.
      </p>
    </div>
  {:else}
    {@render children()}
  {/if}
</AdminShell>
