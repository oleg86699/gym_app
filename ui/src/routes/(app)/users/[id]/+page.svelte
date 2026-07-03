<script lang="ts">
  import { ArrowLeft } from 'lucide-svelte'
  import { goto } from '$app/navigation'
  import { page } from '$app/state'
  import { onMount } from 'svelte'

  import TagAccessPicker from '$lib/components/TagAccessPicker.svelte'

  import {
    groups as groupsApi,
    projects as projectsApi,
    roles as rolesApi,
    users as usersApi,
    wpSites as wpSitesApi,
  } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { Group, Project, ProjectListItem, Role, User, UserDetail } from '$lib/api/types'
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

  // ─── tag-access RBAC ────────────────────────────────────────────────
  // null = без ограничения (наследует потолок группы); [] = нет доступа ни к
  // одному тегу; [..] = только выбранные. availableTags приходит из
  // credential-tags, уже сужен бэкендом до потолка текущего юзера-редактора —
  // group_admin физически не увидит теги вне своей группы.
  let availableTags = $state<string[]>([])
  let tagInfo = $state<Record<string, string>>({})  // тег → «N сайт.»
  let f_tags_restricted = $state(false)   // включён ли allowlist (bind → TagAccessPicker)
  let f_allowed_tags = $state<string[]>([]) // выбранные теги (когда restricted)

  let isSuper = $derived($currentUser?.is_super_admin ?? false)
  let isSelf = $derived($currentUser?.id === userId)
  let isGroupAdmin = $derived(
    $currentUser?.roles?.some((r) => r.name === 'group_admin') ?? false,
  )
  // Кто может править теги этого юзера: super_admin — любому; group_admin —
  // только юзеру своей группы. Зеркалит гейт в бэкенде.
  let canEditTags = $derived(
    isSuper ||
      (isGroupAdmin &&
        !!$currentUser?.group &&
        $currentUser?.group?.id === (user?.group?.id ?? null)),
  )

  // ─── Проекты во владении + переназначение (super_admin only) ────────
  // Деривим из уже загруженного allProjects (тот же источник, что и «Project
  // access» выше) — без отдельного фетча и гонок с загрузкой currentUser.
  let allUsers = $state<User[]>([])
  // project_id → выбранный в строке новый владелец (по умолчанию текущий = userId)
  let rowTarget = $state<Record<number, number>>({})
  let reassignBusy = $state<number | null>(null)
  let ownedProjects = $derived(
    (allProjects as ProjectListItem[]).filter((p) => p.owner.id === userId),
  )

  function userName(id: number): string {
    return allUsers.find((u) => u.id === id)?.username ?? String(id)
  }

  async function reassignOne(p: ProjectListItem, targetId: number) {
    if (targetId === userId) return
    reassignBusy = p.id
    try {
      await projectsApi.reassignOwner(p.id, targetId)
      showToast('success', `«${p.name}» → @${userName(targetId)}`)
      await loadAux() // allProjects перечитается → проект уйдёт из ownedProjects
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      reassignBusy = null
    }
  }

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
      // tag-access: null → без ограничения; массив → allowlist включён
      f_tags_restricted = u.allowed_tags !== null
      f_allowed_tags = u.allowed_tags ?? []
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  async function loadAux() {
    try {
      const [g, r, p, u] = await Promise.all([
        groupsApi.list().catch(() => [] as Group[]),
        rolesApi.list().catch(() => [] as Role[]),
        projectsApi.list({ limit: 200 }).catch(() => ({ items: [] as Project[], next_cursor: null, has_more: false })),
        usersApi.list({ limit: 200 }).catch(() => ({ items: [] as User[], next_cursor: null, has_more: false })),
      ])
      allGroups = g
      // Роль supplier назначается только через «Доступы поставщиков», не вручную.
      allRoles = r.filter((role) => role.name !== 'supplier')
      allProjects = p.items
      allUsers = u.items
    } catch {
      /* noop */
    }
    // Доступные теги — уже сужены бэкендом до потолка редактора (group_admin
    // видит только теги своей группы). Отдельный try, чтобы 403 не ронял остальное.
    try {
      const stats = await wpSitesApi.credentialTagsStats()
      availableTags = stats.map((s) => s.tag)
      tagInfo = Object.fromEntries(stats.map((s) => [s.tag, `${s.sites} сайт.`]))
    } catch {
      availableTags = []
      tagInfo = {}
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

    // tag-access: желаемое значение (restricted → массив, иначе null); шлём только
    // при изменении и если есть право (canEditTags). null явно передаётся, поэтому
    // отличаем «не менять» (undefined) от «снять ограничение» (null).
    const desiredTags: string[] | null = f_tags_restricted ? f_allowed_tags : null
    const tagsChanged =
      canEditTags && !allowedTagsEqual(desiredTags, user.allowed_tags)

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
        allowed_tags: tagsChanged ? desiredTags : undefined,
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

  // Сравнение allowlist'ов тегов с учётом null (= без ограничения).
  function allowedTagsEqual(a: string[] | null, b: string[] | null): boolean {
    if (a === null || b === null) return a === b
    if (a.length !== b.length) return false
    const sa = [...a].sort()
    const sb = [...b].sort()
    return sa.every((v, i) => v === sb[i])
  }

  // Какие проекты дают группа/owner и т.п. — для отображения "уже есть доступ".
  // Зеркалит backend can_view_project: доступ по owner_group получает ТОЛЬКО
  // group_admin своей группы; обычному члену группы owner_group НЕ даёт доступ —
  // только явный share проекта на его группу (shared_with_groups).
  function inheritedAccess(p: Project): string | null {
    const u = user
    if (!u) return null
    if (p.owner.id === u.id) return 'owner'
    const isGroupAdmin = u.roles?.some((r) => r.name === 'group_admin') ?? false
    if (isGroupAdmin && u.group && p.owner_group?.id === u.group.id)
      return `group #${u.group.name} (admin)`
    if (u.group && p.shared_with_groups.some((g) => g.id === u.group?.id))
      return `shared with #${u.group.name}`
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
    <form id="user-edit-form" onsubmit={save} class="space-y-6">
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
          Отметь проекты → выдать этому юзеру индивидуальный доступ.
          <span class="font-medium text-indigo-600">фиолетовый</span> — он владелец;
          <span class="font-medium text-emerald-600">зелёный</span> — доступ уже есть (через группу или индивидуальный share);
          серый — доступа нет. Owner/группа — read-only (галка заблокирована).
        </p>

        {#if allProjects.length === 0}
          <p class="mt-3 text-sm text-slate-400">No projects in system</p>
        {:else}
          <div class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {#each allProjects as p}
              {@const inherited = inheritedAccess(p)}
              {@const isOwner = inherited === 'owner'}
              {@const indivShared = !inherited && f_project_ids.includes(p.id)}
              {@const hasShare = !isOwner && (!!inherited || indivShared)}
              <label class="flex items-center gap-2 rounded-md border px-3 py-2 text-sm"
                     class:bg-indigo-50={isOwner}
                     class:border-indigo-200={isOwner}
                     class:bg-emerald-50={hasShare}
                     class:border-emerald-200={hasShare}
                     class:bg-slate-50={!isOwner && !hasShare}
                     class:border-slate-200={!isOwner && !hasShare}>
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
                  <div class="flex flex-wrap items-center gap-1.5">
                    <span class="font-medium text-slate-900">{p.name}</span>
                    {#if isOwner}
                      <span class="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-indigo-700">владелец</span>
                    {:else if inherited}
                      <span class="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">{inherited}</span>
                    {:else if indivShared}
                      <span class="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium uppercase text-emerald-700">shared</span>
                    {/if}
                  </div>
                  <div class="text-xs text-slate-400">@{p.owner.username}</div>
                </div>
              </label>
            {/each}
          </div>
        {/if}
      </section>

      <!-- Tag access (RBAC) block -->
      {#if canEditTags}
        <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
          <h2 class="text-base font-medium text-slate-900">Доступ по тегам батчей</h2>
          <p class="mt-1 text-xs text-slate-500">
            Ограничь, какие теги (батчи сайтов) этот юзер может использовать при постинге.
            По умолчанию — все теги. Итог = пересечение с потолком его группы.
            {#if isGroupAdmin && !isSuper}
              Тебе доступны только теги твоей команды.
            {/if}
          </p>

          <TagAccessPicker availableTags={availableTags} tagInfo={tagInfo}
                           bind:restricted={f_tags_restricted}
                           bind:selected={f_allowed_tags} />
        </section>
      {/if}
    </form>

    <!-- Проекты во владении + переназначение (super_admin only) -->
    {#if isSuper}
      <section class="rounded-lg border border-violet-200 bg-white p-6 shadow-sm">
        <div class="flex items-center gap-2">
          <h2 class="text-base font-medium text-slate-900">Проекты во владении</h2>
          <span class="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold uppercase text-violet-700">super admin</span>
          <span class="text-xs text-slate-400">{ownedProjects.length}</span>
        </div>
        <p class="mt-1 text-xs text-slate-500">
          Проекты, где @{user.username} — владелец. Переназначь их другому пользователю (например, когда
          сотрудник ушёл) — прогоны, домены, креды и общий доступ перейдут вместе с проектом, ничего не теряется.
        </p>

        {#if ownedProjects.length === 0}
          <p class="mt-3 text-sm text-slate-400">У пользователя нет проектов во владении.</p>
        {:else}
          <div class="mt-3 overflow-hidden rounded-md border border-slate-200">
            <table class="min-w-full text-sm">
              <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
                <tr>
                  <th class="px-3 py-2">Проект</th>
                  <th class="px-3 py-2">Владелец</th>
                  <th class="w-px px-3 py-2"></th>
                </tr>
              </thead>
              <tbody class="divide-y divide-slate-100">
                {#each ownedProjects as p (p.id)}
                  {@const target = rowTarget[p.id] ?? userId}
                  <tr class="hover:bg-slate-50">
                    <td class="px-3 py-2">
                      <a href={`/projects/${p.id}`} class="font-medium text-brand-600 hover:underline">{p.name}</a>
                      {#if !p.is_active}<span class="ml-1 text-[10px] uppercase text-slate-400">inactive</span>{/if}
                    </td>
                    <td class="px-3 py-2">
                      <select
                        value={target}
                        onchange={(e) => (rowTarget = { ...rowTarget, [p.id]: Number(e.currentTarget.value) })}
                        class="rounded-md border border-slate-300 px-2 py-1 text-sm">
                        {#each allUsers as u}
                          <option value={u.id}>@{u.username}{u.id === userId ? ' · текущий' : ''}</option>
                        {/each}
                      </select>
                    </td>
                    <td class="px-3 py-2 text-right">
                      <button type="button" onclick={() => reassignOne(p, target)}
                              disabled={target === userId || reassignBusy !== null}
                              class="whitespace-nowrap rounded border border-violet-300 px-2 py-1 text-xs font-medium text-violet-700 hover:bg-violet-50 disabled:opacity-50">
                        {reassignBusy === p.id ? '…' : 'Переназначить →'}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    {/if}

    <div class="flex items-center justify-between">
      <button type="button" onclick={() => goto('/users')}
              class="text-sm text-slate-500 hover:text-slate-700">
        Cancel
      </button>
      <button type="submit" form="user-edit-form" disabled={saving}
              class="rounded-md bg-brand-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-brand-700 disabled:bg-slate-300">
        {saving ? 'Saving…' : 'Save changes'}
      </button>
    </div>
  {/if}
</div>
