<script lang="ts">
  import { page } from '$app/state'
  import { currentUser, hasPageAccess } from '$lib/stores/user'

  interface NavItem {
    path: string
    label: string
    section?: string
  }

  const navItems: NavItem[] = [
    { path: '/dashboard', label: 'Dashboard' },
    { path: '/projects', label: 'Projects', section: 'Work' },
    { path: '/runs', label: 'Runs', section: 'Work' },
    { path: '/queue', label: 'Global Queue', section: 'Work' },
    { path: '/texts', label: 'Texts', section: 'Work' },
    { path: '/wp-sites', label: 'WP Sites', section: 'WP Validate' },
    { path: '/batches', label: 'Batches', section: 'WP Validate' },
    { path: '/users', label: 'Users', section: 'Access' },
    { path: '/groups', label: 'Groups', section: 'Access' },
    { path: '/invitations', label: 'Invitations', section: 'Access' },
    { path: '/roles', label: 'Roles', section: 'Access' },
    { path: '/pages', label: 'Pages', section: 'Access' },
    { path: '/settings', label: 'Settings', section: 'System' },
    { path: '/health', label: 'Health', section: 'System' },
    { path: '/audit-log', label: 'Audit log', section: 'System' },
    { path: '/proxies', label: 'Proxies', section: 'Services' },
    { path: '/ai-settings', label: 'AI Settings', section: 'Services' },
    { path: '/profile', label: 'Profile', section: 'Account' },
  ]

  let user = $derived($currentUser)

  let visibleItems = $derived(navItems.filter((i) => hasPageAccess(user, i.path)))

  // Группировка по секциям
  let grouped = $derived.by(() => {
    const groups: { section: string; items: NavItem[] }[] = []
    for (const item of visibleItems) {
      const section = item.section ?? ''
      const last = groups[groups.length - 1]
      if (last && last.section === section) last.items.push(item)
      else groups.push({ section, items: [item] })
    }
    return groups
  })

  let currentPath = $derived(page.url.pathname)
</script>

<aside class="flex w-60 flex-col border-r border-slate-200 bg-white">
  <div class="flex h-14 items-center px-5">
    <a href="/dashboard" class="text-lg font-semibold tracking-tight text-slate-900">
      gym<span class="text-brand-600">_app</span>
    </a>
  </div>

  <nav class="flex-1 space-y-6 px-3 py-4 text-sm">
    {#each grouped as group}
      <div>
        {#if group.section}
          <div class="px-2 pb-1 text-xs font-medium uppercase tracking-wider text-slate-400">
            {group.section}
          </div>
        {/if}
        <ul class="space-y-0.5">
          {#each group.items as item}
            {@const isActive = currentPath === item.path || currentPath.startsWith(item.path + '/')}
            <li>
              <a
                href={item.path}
                class="flex items-center rounded-md px-3 py-1.5 transition-colors"
                class:bg-brand-50={isActive}
                class:text-brand-700={isActive}
                class:font-medium={isActive}
                class:text-slate-600={!isActive}
                class:hover:bg-slate-100={!isActive}
                class:hover:text-slate-900={!isActive}
              >
                {item.label}
              </a>
            </li>
          {/each}
        </ul>
      </div>
    {/each}
  </nav>

  <div class="px-5 py-4 text-xs text-slate-400">
    <div>gym_app · stage 1</div>
    <div class="mt-1">{user?.username ?? '—'}</div>
  </div>
</aside>
