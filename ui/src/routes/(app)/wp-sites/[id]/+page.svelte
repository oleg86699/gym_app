<script lang="ts">
  import { ArrowLeft, ExternalLink, AlertTriangle, Eye, EyeOff, Copy } from 'lucide-svelte'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import { wpSites as sitesApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { SiteAnalytics, SiteEvent, WpSiteDetail } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let siteId = $derived(Number(page.params.id))
  let site = $state<WpSiteDetail | null>(null)
  let analytics = $state<SiteAnalytics | null>(null)
  let events = $state<SiteEvent[]>([])
  let loading = $state(true)

  let isSuper = $derived($currentUser?.is_super_admin ?? false)

  async function load() {
    loading = true
    try {
      const [s, a, ev] = await Promise.all([
        sitesApi.get(siteId, { include_password: isSuper }),
        sitesApi.analytics(siteId),
        sitesApi.events(siteId, 50).catch(() => [] as SiteEvent[]),
      ])
      site = s
      analytics = a
      events = ev
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(load)

  // Password reveal toggle per cred
  let revealedCredIds = $state<Set<number>>(new Set())
  function toggleReveal(credId: number) {
    if (revealedCredIds.has(credId)) revealedCredIds.delete(credId)
    else revealedCredIds.add(credId)
    revealedCredIds = new Set(revealedCredIds)
  }
  async function copyToClipboard(text: string, label: string) {
    try {
      await navigator.clipboard.writeText(text)
      showToast('success', `${label} copied`)
    } catch {
      showToast('error', 'Clipboard access denied')
    }
  }

  function fullTs(iso: string | null | undefined): string {
    return iso ? new Date(iso).toLocaleString() : '—'
  }
  function relTime(iso: string | null | undefined): string {
    if (!iso) return '—'
    const diff = Date.now() - new Date(iso).getTime()
    if (diff < 0) return 'future'
    const s = Math.floor(diff / 1000)
    if (s < 60) return `${s}s ago`
    const m = Math.floor(s / 60)
    if (m < 60) return `${m}m ago`
    const h = Math.floor(m / 60)
    if (h < 48) return `${h}h ago`
    const d = Math.floor(h / 24)
    if (d < 30) return `${d}d ago`
    const mo = Math.floor(d / 30)
    return mo < 12 ? `${mo}mo ago` : `${Math.floor(mo / 12)}y ago`
  }
</script>

<div class="space-y-5">
  <div>
    <a href="/wp-sites" class="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700">
      <ArrowLeft size={14} /> WP Sites
    </a>
    {#if loading}
      <h1 class="mt-1 text-2xl font-semibold text-slate-900">Loading…</h1>
    {:else if site}
      <div class="mt-1 flex flex-wrap items-baseline gap-3">
        <h1 class="font-mono text-2xl font-semibold text-slate-900">{site.domain}</h1>
        <a href={`https://${site.domain}/`} target="_blank" rel="noopener" class="inline-flex items-center gap-1 text-sm text-brand-600 hover:underline">
          <ExternalLink size={14} /> visit
        </a>
        <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase"
              class:bg-emerald-100={site.is_active}
              class:text-emerald-700={site.is_active}
              class:bg-red-100={!site.is_active && site.auto_disabled_at}
              class:text-red-700={!site.is_active && site.auto_disabled_at}
              class:bg-slate-200={!site.is_active && !site.auto_disabled_at}
              class:text-slate-600={!site.is_active && !site.auto_disabled_at}>
          {site.is_active ? 'active' : (site.auto_disabled_at ? `auto-off · ${site.last_site_failure_kind ?? '?'}` : 'off')}
        </span>
        {#if site.language}
          <span class="rounded-md bg-slate-100 px-2 py-0.5 text-[11px] uppercase text-slate-700"
                title={site.language_detected_at ? 'detected ' + relTime(site.language_detected_at) : ''}>
            lang: {site.language}
          </span>
        {/if}
      </div>
      {#if site.note}
        <p class="mt-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700">{site.note}</p>
      {/if}
    {:else}
      <p class="mt-1 text-sm text-red-600">Site not found</p>
    {/if}
  </div>

  {#if site && analytics}
    <!-- Key numbers -->
    <div class="grid gap-3 sm:grid-cols-2 md:grid-cols-4 xl:grid-cols-6">
      <div class="rounded-lg border border-slate-200 bg-white p-4"
           title="Всего успешных публикаций со всех проектов">
        <div class="text-xs uppercase tracking-wider text-slate-500">Posts total</div>
        <div class="mt-1 text-2xl font-semibold text-slate-900">{analytics.posts_total}</div>
      </div>
      <div class="rounded-lg border border-emerald-200 bg-emerald-50/40 p-4">
        <div class="text-xs uppercase tracking-wider text-emerald-700">Last 24h</div>
        <div class="mt-1 text-2xl font-semibold text-emerald-700">{analytics.posts_24h}</div>
      </div>
      <div class="rounded-lg border border-emerald-200 bg-emerald-50/30 p-4">
        <div class="text-xs uppercase tracking-wider text-emerald-700">Last 7 days</div>
        <div class="mt-1 text-2xl font-semibold text-emerald-700">{analytics.posts_7d}</div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4"
           title="Сколько разных проектов использовали этот сайт">
        <div class="text-xs uppercase tracking-wider text-slate-500">Projects used</div>
        <div class="mt-1 text-2xl font-semibold text-slate-900">{analytics.distinct_projects}</div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4"
           title="Сколько разных credentials с этого сайта реально работали">
        <div class="text-xs uppercase tracking-wider text-slate-500">Creds used</div>
        <div class="mt-1 text-2xl font-semibold text-slate-900">{analytics.distinct_credentials_used}</div>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-4">
        <div class="text-xs uppercase tracking-wider text-slate-500">Last post</div>
        <div class="mt-1 text-sm font-medium text-slate-800" title={fullTs(analytics.last_posted_at)}>
          {relTime(analytics.last_posted_at)}
        </div>
        {#if analytics.first_posted_at && analytics.first_posted_at !== analytics.last_posted_at}
          <div class="mt-0.5 text-[10px] text-slate-400" title={fullTs(analytics.first_posted_at)}>
            first {relTime(analytics.first_posted_at)}
          </div>
        {/if}
      </div>
    </div>

    <!-- Site debug strip -->
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-700">Site info</h2>
      <div class="mt-2 grid gap-x-6 gap-y-1.5 text-xs text-slate-700 md:grid-cols-2 lg:grid-cols-3">
        <div><span class="text-slate-400">Created:</span> <span>{fullTs(site.created_at)}</span></div>
        <div><span class="text-slate-400">Hint path:</span> <code class="ml-1 rounded bg-slate-100 px-1">{site.hint_path ?? '—'}</code></div>
        <div><span class="text-slate-400">Hint port:</span> <code class="ml-1 rounded bg-slate-100 px-1">{site.hint_port ?? '—'}</code></div>
        <div class="md:col-span-2">
          <span class="text-slate-400">XML-RPC cache:</span>
          {#if site.last_working_url}
            <code class="ml-1 rounded bg-emerald-50 px-1 text-emerald-700" title={site.last_working_url}>
              {site.last_working_url.length > 70 ? site.last_working_url.slice(0, 70) + '…' : site.last_working_url}
            </code>
            <span class="ml-1 text-slate-400">({relTime(site.last_working_at)})</span>
          {:else}
            <span class="ml-1 text-slate-400">— (will discover on next call)</span>
          {/if}
        </div>
        {#if site.wp_version}
          <div><span class="text-slate-400">WP version:</span> <code class="ml-1 rounded bg-slate-100 px-1">{site.wp_version}</code></div>
        {/if}
        {#if site.active_theme}
          <div><span class="text-slate-400">Theme:</span> <code class="ml-1 rounded bg-slate-100 px-1">{site.active_theme}</code></div>
        {/if}
        {#if site.cf_protected}
          <div class="text-amber-700"><AlertTriangle size={11} class="inline" /> CF protected</div>
        {/if}
        {#if site.file_editing_disabled !== null && site.file_editing_disabled !== undefined}
          <div>
            <span class="text-slate-400">File editing:</span>
            <span>{site.file_editing_disabled ? 'disabled (DISALLOW_FILE_EDIT)' : 'enabled'}</span>
          </div>
        {/if}
        {#if (site.consecutive_site_failures ?? 0) > 0 || site.auto_disabled_at}
          <div class="md:col-span-3">
            <span class="text-slate-400">Site fails:</span>
            <span class="ml-1 text-amber-700">{site.consecutive_site_failures ?? 0}</span>
            {#if site.last_site_failure_kind}
              <code class="ml-2 rounded bg-amber-50 px-1 text-amber-700">{site.last_site_failure_kind}</code>
            {/if}
            {#if site.last_site_failure_at}
              <span class="ml-1 text-slate-400">({relTime(site.last_site_failure_at)})</span>
            {/if}
            {#if site.auto_disabled_at}
              <span class="ml-2 rounded bg-red-50 px-1 text-red-700">auto-disabled {relTime(site.auto_disabled_at)}</span>
            {/if}
          </div>
        {/if}
      </div>
    </div>

    <!-- Credentials -->
    {#if site.credentials.length > 0}
      <section>
        <h2 class="mb-2 text-lg font-medium text-slate-900">Credentials ({site.credentials.length})</h2>
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-3 py-2">Login</th>
                {#if isSuper}<th class="px-3 py-2">Password</th>{/if}
                <th class="px-3 py-2 text-center">Valid</th>
                <th class="px-3 py-2">Last validated</th>
                <th class="px-3 py-2 text-center">Uses</th>
                <th class="px-3 py-2 text-center">Last used</th>
                <th class="px-3 py-2">Tags</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each site.credentials as c}
                <tr class="hover:bg-slate-50" class:bg-blue-50={c.provisioned}>
                  <td class="px-3 py-2">
                    <span class="font-mono text-xs">{c.login}</span>
                    {#if c.provisioned}
                      <span class="ml-1 rounded-full bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-700"
                            title={`Создан нами (provision-author${c.provisioned_via ? ', через ' + c.provisioned_via : ''})`}>＋ наш</span>
                    {/if}
                    {#if isSuper}
                      <button onclick={() => copyToClipboard(c.login, 'Login')}
                              title="Copy login"
                              class="ml-1 text-slate-400 hover:text-slate-700">
                        <Copy size={11} class="inline" />
                      </button>
                    {/if}
                  </td>
                  {#if isSuper}
                    {@const isRevealed = revealedCredIds.has(c.id)}
                    <td class="px-3 py-2 text-xs">
                      {#if c.password}
                        <span class="select-all font-mono">{isRevealed ? c.password : '••••••••'}</span>
                        <button onclick={() => toggleReveal(c.id)}
                                title={isRevealed ? 'Hide' : 'Show'}
                                class="ml-1 text-slate-400 hover:text-slate-700">
                          {#if isRevealed}<EyeOff size={11} class="inline" />{:else}<Eye size={11} class="inline" />{/if}
                        </button>
                        <button onclick={() => copyToClipboard(c.password!, 'Password')}
                                title="Copy password"
                                class="ml-1 text-slate-400 hover:text-slate-700">
                          <Copy size={11} class="inline" />
                        </button>
                      {:else}
                        <span class="text-slate-300" title="Не удалось расшифровать">—</span>
                      {/if}
                    </td>
                  {/if}
                  <td class="px-3 py-2 text-center">
                    {#if !c.is_valid}
                      <span class="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-medium uppercase text-red-700"
                            title={c.last_validation_kind === 'auth_invalid' || c.last_validation_kind === 'permission_denied' ? 'Wrong login/password' : 'Marked invalid'}>
                        {c.last_validation_kind === 'auth_invalid' || c.last_validation_kind === 'permission_denied' ? 'auth fail' : 'invalid'}
                      </span>
                    {:else if c.last_validation_kind === 'manual_valid'}
                      <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium uppercase text-emerald-700"
                            title="Помечен валидным вручную (import без проверки)">manual</span>
                    {:else if c.last_validation_kind === 'ok'}
                      <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium uppercase text-emerald-700"
                            title="XML-RPC: OK">valid</span>
                    {:else if c.can_admin_login === true}
                      <span class="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium uppercase text-emerald-700"
                            title="Admin login (Tier 2): OK">valid · admin</span>
                    {:else if c.last_validated_at}
                      <span class="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium uppercase text-amber-700"
                            title={c.last_validation_kind || 'Ни один канал не подтвердил'}>transient</span>
                    {:else}
                      <span class="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium uppercase text-slate-500">pending</span>
                    {/if}
                  </td>
                  <td class="px-3 py-2 text-xs text-slate-500">{relTime(c.last_validated_at)}</td>
                  <td class="px-3 py-2 text-center text-xs text-slate-600">{c.amount_use}</td>
                  <td class="px-3 py-2 text-center text-xs text-slate-500">{relTime(c.last_used_at)}</td>
                  <td class="px-3 py-2 text-xs">
                    {#if c.tags && c.tags.length}
                      <div class="flex flex-wrap gap-0.5">
                        {#each c.tags as t}
                          <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px]">{t}</span>
                        {/each}
                      </div>
                    {:else}—{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
    {/if}

    <!-- Posts list -->
    <section>
      <h2 class="mb-2 text-lg font-medium text-slate-900">
        Recent posts {analytics.posts_total > 0 ? `(${analytics.recent_posts.length} of ${analytics.posts_total})` : ''}
      </h2>
      {#if analytics.recent_posts.length === 0}
        <div class="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">
          No posts published from this site yet.
        </div>
      {:else}
        <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-3 py-2">When</th>
                <th class="px-3 py-2">Title</th>
                <th class="px-3 py-2">URL</th>
                <th class="px-3 py-2">Project</th>
                <th class="px-3 py-2">Run</th>
                <th class="px-3 py-2" title="Кто запустил run (создатель прогона)">Created by</th>
                <th class="px-3 py-2" title="WP-login через который запостили (WpCredential.login)">WP login</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each analytics.recent_posts as p}
                <tr class="hover:bg-slate-50">
                  <td class="px-3 py-2 text-xs text-slate-500" title={fullTs(p.posted_at)}>{relTime(p.posted_at)}</td>
                  <td class="px-3 py-2 text-slate-700" title={p.text_title ?? ''}>
                    {p.text_title && p.text_title.length > 60 ? p.text_title.slice(0, 60) + '…' : (p.text_title ?? '—')}
                  </td>
                  <td class="px-3 py-2 text-xs">
                    {#if p.posted_url}
                      <a href={p.posted_url} target="_blank" rel="noopener" class="inline-flex items-center gap-1 text-brand-600 hover:underline">
                        <ExternalLink size={11} /> open
                      </a>
                    {:else}—{/if}
                  </td>
                  <td class="px-3 py-2 text-xs">
                    <a href={`/projects/${p.project_id}`} class="text-brand-600 hover:underline">{p.project_name}</a>
                  </td>
                  <td class="px-3 py-2 text-xs">
                    <a href={`/runs/${p.posting_run_id}`} class="text-brand-600 hover:underline">{p.run_name}</a>
                  </td>
                  <td class="px-3 py-2 text-xs text-slate-700">
                    {#if p.run_creator_username}@{p.run_creator_username}{:else}<span class="text-slate-300">—</span>{/if}
                  </td>
                  <td class="px-3 py-2 font-mono text-xs text-slate-600">{p.credential_login ?? '—'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        {#if analytics.posts_total > analytics.recent_posts.length}
          <p class="mt-2 text-xs text-slate-400">Showing latest {analytics.recent_posts.length} of {analytics.posts_total} posts.</p>
        {/if}
      {/if}
    </section>

    <!-- Error history (site_events) -->
    <section class="mt-6">
      <h2 class="text-lg font-medium text-slate-900">
        История ошибок
        <span class="ml-2 text-sm font-normal text-slate-500">{events.length}</span>
      </h2>
      {#if events.length === 0}
        <div class="mt-2 rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-400">
          Ошибок не зафиксировано 🎉
        </div>
      {:else}
        <div class="mt-2 overflow-hidden rounded-lg border border-slate-200 bg-white">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
              <tr>
                <th class="px-4 py-2">When</th>
                <th class="px-4 py-2">Source</th>
                <th class="px-4 py-2">Error</th>
                <th class="px-4 py-2">Message</th>
                <th class="px-4 py-2">Run</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              {#each events as ev (ev.id)}
                <tr class="hover:bg-slate-50">
                  <td class="px-4 py-2 text-xs text-slate-500" title={fullTs(ev.created_at)}>{relTime(ev.created_at)}</td>
                  <td class="px-4 py-2">
                    <span class="rounded px-1.5 py-0.5 text-[10px] uppercase {ev.source === 'posting' ? 'bg-brand-50 text-brand-700' : 'bg-slate-100 text-slate-600'}">{ev.source}</span>
                  </td>
                  <td class="px-4 py-2">
                    <code class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700">{ev.error_kind}</code>
                  </td>
                  <td class="px-4 py-2 max-w-md truncate text-xs text-red-600" title={ev.error_message ?? ''}>{ev.error_message ?? '—'}</td>
                  <td class="px-4 py-2 text-xs">
                    {#if ev.posting_run_id}
                      <a href={`/runs/${ev.posting_run_id}`} class="text-brand-600 hover:underline">#{ev.posting_run_id}</a>
                    {:else}—{/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
        <p class="mt-2 text-[11px] text-slate-400">
          Append-only лог: и валидация, и постинг. Старые события архивируются помесячно.
        </p>
      {/if}
    </section>
  {/if}
</div>
