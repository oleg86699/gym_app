<script lang="ts">
  import { ArrowLeft } from 'lucide-svelte'
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import {
    groups as groupsApi,
    projects as projectsApi,
    roles as rolesApi,
    users as usersApi,
  } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { Group, Project, Role, UserDetail } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let userId = $derived(Number(page.params.id))

  let user = $state<UserDetail | null>(null)
  let allGroups = $state<Group[]>([])
  let allRoles = $state<Role[]>([])
  let allProjects = $state<Project[]>([])
  let loading = $state(true)
  let saving = $state(false)

  // Editable form state (отдельные, чтобы не мутировать user напрямую)
  let f_username = $state('')
  let f_email = $state('')
  let f_full_name = $state('')
  let f_password = $state('')
  let f_group_id = $state<number | null>(null)
  let f_is_active = $state(true)
  let f_role_ids = $state<number[]>([])
  let f_project_ids = $state<number[]>([])

  let isSuper = $derived($currentUser?.is_super_admin ?? false)
  let isSelf = $derived($currentUser?.id === userId)

  async function load() {
    loading = true
    try {
      const u = await usersApi.get(userId)
      user = u
      // hydrate form state
      f_username = u.username
      f_email = u.email ?? ''
      f_full_name = u.full_name ?? ''
      f_password = ''
      f_group_id = u.group?.id ?? null
      f_is_active = u.is_active
      f_role_ids = u.roles.map((r) => r.id)
      f_project_ids = u.shared_projects.map((p) => p.id)
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadAux() {
    try {
      const [g, r, p] = await Promise.all([
        groupsApi.list().catch(() => [] as Group[]),
        rolesApi.list().catch(() => [] as Role[]),
        projectsApi.list({ limit: 200 }).catch(() => ({ items: [] as Project[], next_cursor: null, has_more: false })),
      ])
      allGroups = g
      allRoles = r
      allProjects = p.items
    } catch {
      /* noop */
    }
  }

  onMount(async () => {
    await Promise.all([load(), loadAux()])
  })

  async function save(e: SubmitEvent) {
    e.preventDefault()
    if (!user) return
    saving = true

    const wasRemovedFromGroup = user.group !== null && f_group_id === null

    try {
      await usersApi.update(user.id, {
        username: f_username !== user.username ? f_username : undefined,
        email: f_email !== (user.email ?? '') ? f_email || undefined : undefined,
        full_name: f_full_name !== (user.full_name ?? '') ? f_full_name : undefined,
        password: f_password ? f_password : undefined,
        is_active: f_is_active !== user.is_active ? f_is_active : undefined,
        group_id: !wasRemovedFromGroup && f_group_id !== user.group?.id ? (f_group_id ?? undefined) : undefined,
        is_remove_from_group: wasRemovedFromGroup ? true : undefined,
        role_ids: !arraysEqual(f_role_ids, user.roles.map((r) => r.id)) ? f_role_ids : undefined,
        project_ids: !arraysEqual(f_project_ids, user.shared_projects.map((p) => p.id))
          ? f_project_ids
          : undefined,
      })
      showToast('success', 'User updated')
      await load()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      saving = false
    }
  }

  function arraysEqual(a: number[], b: number[]): boolean {
    if (a.length !== b.length) return false
    const sa = [...a].sort()
    const sb = [...b].sort()
    return sa.every((v, i) => v === sb[i])
  }

  // Какие проекты дают группа/owner и т.п. — для отображения "уже есть доступ"
  function inheritedAccess(p: Project): string | null {
    if (!user) return null
    if (p.owner.id === user.id) return 'owner'
    if (user.group && p.owner_group?.id === user.group.id) return `group #${user.group.name}`
    if (user.group && p.shared_with_groups.some((g) => g.id === user.group?.id))
      return `shared with #${user.group.name}`
    return null
  }
</script>

<div class="space-y-6">
  <div>
    <a href="/users" class="text-sm text-slate-500 hover:text-slate-700"><ArrowLeft size={14} class="inline-block align-text-bottom" /> Users</a>
    <h1 class="mt-1 text-2xl font-semibold text-slate-900">
      Edit user {user ? `@${user.username}` : '…'}
    </h1>
    <p class="mt-1 text-sm text-slate-500">Update access, roles, and project sharing.</p>
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if !user}
    <div class="rounded-lg border border-amber-200 bg-amber-50 p-8 text-center text-sm text-amber-700">
      User not found
    </div>
  {:else}
    <form onsubmit={save} class="space-y-6">
      <!-- Identity block -->
      <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div>
            <label for="f_username" class="block text-sm font-medium text-slate-700">Username</label>
            <input id="f_username" type="text" bind:value={f_username} required minlength="3"
                   disabled={!isSuper && !isSelf}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-50" />
          </div>
          <div>
            <label for="f_email" class="block text-sm font-medium text-slate-700">Email</label>
            <input id="f_email" type="email" bind:value={f_email}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
          <div>
            <label for="f_fullname" class="block text-sm font-medium text-slate-700">Full name</label>
            <input id="f_fullname" type="text" bind:value={f_full_name}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
          <div>
            <label for="f_password" class="block text-sm font-medium text-slate-700">
              Password <span class="text-slate-400">(optional reset)</span>
            </label>
            <input id="f_password" type="password" bind:value={f_password} minlength="8"
                   placeholder="leave empty to keep current"
                   autocomplete="new-password"
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          </div>
          <div>
            <label for="f_group" class="block text-sm font-medium text-slate-700">Group</label>
            <select id="f_group" bind:value={f_group_id} disabled={!isSuper}
                    class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-50">
              <option value={null}>— none —</option>
              {#each allGroups as g}
                <option value={g.id}>{g.name}</option>
              {/each}
            </select>
          </div>
          <div>
            <label for="f_status" class="block text-sm font-medium text-slate-700">Status</label>
            <select id="f_status" bind:value={f_is_active}
                    class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm">
              <option value={true}>Active</option>
              <option value={false}>Inactive</option>
            </select>
          </div>
        </div>
      </section>

      <!-- Roles block -->
      {#if allRoles.length > 0}
        <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <h2 class="text-base font-medium text-slate-900">Roles</h2>
          <p class="mt-1 text-xs text-slate-500">
            Чекбоксы — наборы прав. {!isSuper ? 'Доступны только assignable роли.' : ''}
          </p>
          <div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {#each allRoles as r}
              <label class="flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
                <input
                  type="checkbox"
                  value={r.id}
                  checked={f_role_ids.includes(r.id)}
                  onchange={(e) => {
                    if (e.currentTarget.checked) f_role_ids = [...f_role_ids, r.id]
                    else f_role_ids = f_role_ids.filter((id) => id !== r.id)
                  }}
                />
                <span>{r.name}</span>
                {#if r.is_system}
                  <span class="text-xs text-slate-400">system</span>
                {/if}
              </label>
            {/each}
          </div>
        </section>
      {/if}

      <!-- Project access block -->
      <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 class="text-base font-medium text-slate-900">Project access</h2>
        <p class="mt-1 text-xs text-slate-500">
          Индивидуальный shared доступ. Проекты, к которым у юзера уже есть доступ через owner или группу — помечены.
        </p>

        {#if allProjects.length === 0}
          <p class="mt-3 text-sm text-slate-400">No projects in system</p>
        {:else}
          <div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {#each allProjects as p}
              {@const inherited = inheritedAccess(p)}
              <label class="flex items-center gap-2 rounded-md border border-slate-200 px-3 py-2 text-sm"
                     class:bg-emerald-50={inherited}
                     class:border-emerald-200={inherited}
                     class:bg-slate-50={!inherited}>
                <input
                  type="checkbox"
                  value={p.id}
                  checked={f_project_ids.includes(p.id) || !!inherited}
                  disabled={!!inherited}
                  onchange={(e) => {
                    if (e.currentTarget.checked) f_project_ids = [...f_project_ids, p.id]
                    else f_project_ids = f_project_ids.filter((id) => id !== p.id)
                  }}
                />
                <div class="flex-1">
                  <div class="font-medium text-slate-900">{p.name}</div>
                  <div class="text-xs text-slate-400">
                    @{p.owner.username}
                    {#if inherited}· via {inherited}{/if}
                  </div>
                </div>
              </label>
            {/each}
          </div>
        {/if}
      </section>

      <div class="flex items-center justify-between">
        <button type="button" onclick={() => goto('/users')}
                class="text-sm text-slate-500 hover:text-slate-700">
          Cancel
        </button>
        <button type="submit" disabled={saving}
                class="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-brand-700 disabled:bg-slate-300">
          {saving ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </form>
  {/if}
</div>
