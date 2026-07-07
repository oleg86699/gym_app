<script lang="ts">
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { auth } from '$lib/api/admin'
  import { currentUser } from '$lib/stores/user'
  import { showToast } from '$lib/stores/toast'

  let user = $derived($currentUser)
  let menuOpen = $state(false)

  async function handleLogout() {
    try {
      await auth.logout()
    } catch {
      // ignore — куку всё равно очистим
    }
    currentUser.set(null)
    showToast('info', 'Logged out')
    await goto('/login', { replaceState: true })
  }

  // Хлебные крошки — пока просто текущий путь
  let breadcrumb = $derived(page.url.pathname.replace(/^\//, '') || 'Dashboard')
</script>

<header class="flex h-14 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-6">
  <div class="text-sm font-medium capitalize text-slate-700">{breadcrumb}</div>

  {#if user}
    <div class="relative">
      <button
        type="button"
        onclick={() => (menuOpen = !menuOpen)}
        class="flex items-center gap-2 rounded-md px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
      >
        <div class="flex h-7 w-7 items-center justify-center rounded-full bg-brand-600 text-xs font-medium text-white">
          {user.username.slice(0, 2).toUpperCase()}
        </div>
        <span>{user.username}</span>
        {#if user.is_super_admin}
          <span class="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-amber-700">
            super
          </span>
        {/if}
      </button>

      {#if menuOpen}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <div
          class="fixed inset-0 z-10"
          onclick={() => (menuOpen = false)}
        ></div>
        <div class="absolute right-0 z-20 mt-1 w-48 rounded-md border border-slate-200 bg-white py-1 shadow-lg">
          <a
            href="/profile"
            class="block px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100"
            onclick={() => (menuOpen = false)}
          >
            Profile
          </a>
          <button
            type="button"
            class="block w-full px-3 py-1.5 text-left text-sm text-slate-700 hover:bg-slate-100"
            onclick={handleLogout}
          >
            Logout
          </button>
        </div>
      {/if}
    </div>
  {/if}
</header>
