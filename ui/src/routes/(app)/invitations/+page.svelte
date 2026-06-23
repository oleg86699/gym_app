<script lang="ts">
  import { ArrowRight, Check } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { groups as groupsApi, invitations as invitesApi, roles as rolesApi } from '$lib/api/admin'
  import SupplierAccessPanel from '$lib/components/SupplierAccessPanel.svelte'
  import { ApiError } from '$lib/api/client'
  import type { CreatedInvitation, GroupListItem, Invitation, Role } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let items = $state<Invitation[]>([])
  let allGroups = $state<GroupListItem[]>([])
  let allRoles = $state<Role[]>([])
  let loading = $state(true)
  let includeUsed = $state(true)

  let isSuper = $derived($currentUser?.is_super_admin ?? false)

  // Create modal
  let createOpen = $state(false)
  let createBusy = $state(false)
  let newGroupId = $state<number | null>(null)
  let newRoleIds = $state<number[]>([])
  let newEmail = $state('')
  let newNote = $state('')
  let newTtlHours = $state(12) // 12 часов

  // Show created invite (one-shot)
  let createdInvite = $state<CreatedInvitation | null>(null)
  let copied = $state(false)

  async function refresh() {
    loading = true
    try {
      const [inv, g, r] = await Promise.all([
        invitesApi.list(includeUsed),
        groupsApi.list().catch(() => [] as GroupListItem[]),
        rolesApi.list().catch(() => [] as Role[]),
      ])
      items = inv
      allGroups = g
      // supplier выдаётся только через «Доступы поставщиков», не обычным приглашением.
      allRoles = r.filter((role) => role.name !== 'supplier')
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(refresh)

  function openCreate() {
    // Дефолтная группа для group_admin — своя
    if (!isSuper && $currentUser?.group) {
      newGroupId = $currentUser.group.id
    } else {
      newGroupId = null
    }
    newRoleIds = []
    newEmail = ''
    newNote = ''
    newTtlHours = 12
    createOpen = true
  }

  async function handleCreate(e: SubmitEvent) {
    e.preventDefault()
    createBusy = true
    try {
      const inv = await invitesApi.create({
        group_id: newGroupId ?? undefined,
        role_ids: newRoleIds.length ? newRoleIds : undefined,
        email: newEmail || undefined,
        note: newNote || undefined,
        ttl_hours: newTtlHours,
      })
      createdInvite = inv
      createOpen = false
      copied = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      createBusy = false
    }
  }

  async function copyUrl() {
    if (!createdInvite) return
    try {
      await navigator.clipboard.writeText(createdInvite.invite_url)
      copied = true
      setTimeout(() => (copied = false), 2000)
    } catch {
      showToast('error', 'Clipboard access denied')
    }
  }

  async function revoke(inv: Invitation) {
    if (!confirm(`Revoke invitation ${inv.token_prefix}…?\n\nЗапись останется в журнале как «revoked».`)) return
    try {
      await invitesApi.revoke(inv.id)
      showToast('success', 'Invitation revoked')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function remove(inv: Invitation) {
    if (!confirm(`Удалить запись invitation ${inv.token_prefix}… ?\n\nПолностью сотрётся из БД, восстановить нельзя.`)) return
    try {
      await invitesApi.remove(inv.id)
      showToast('success', 'Invitation deleted')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function cleanupInactive() {
    if (!isSuper) return
    const targets = items.filter((i) => i.is_revoked || i.used_at || new Date(i.expires_at) < new Date())
    if (targets.length === 0) {
      showToast('info', 'Нет неактивных инвайтов для очистки')
      return
    }
    if (!confirm(`Удалить ${targets.length} неактивных invitation (revoked / used / expired)?\n\nЭто необратимо.`)) return
    try {
      await Promise.all(targets.map((i) => invitesApi.remove(i.id)))
      showToast('success', `Удалено ${targets.length} invitation(s)`)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  function statusOf(inv: Invitation): { label: string; class: string } {
    if (inv.is_revoked) return { label: 'revoked', class: 'bg-slate-200 text-slate-600' }
    if (inv.used_at) return { label: 'used', class: 'bg-brand-100 text-brand-700' }
    if (new Date(inv.expires_at) < new Date()) return { label: 'expired', class: 'bg-amber-100 text-amber-700' }
    return { label: 'active', class: 'bg-emerald-100 text-emerald-700' }
  }
</script>

<div class="space-y-6">
  {#if isSuper}
    <SupplierAccessPanel />
  {/if}

  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-2xl font-semibold text-slate-900">Invitations</h1>
      <p class="mt-1 text-sm text-slate-500">
        Создавай безопасные ссылки для регистрации. Token показывается ОДИН раз.
      </p>
    </div>
    <div class="flex items-center gap-2">
      {#if isSuper}
        <button onclick={cleanupInactive}
                class="rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50"
                title="Удалить все revoked/used/expired записи">
          Очистить страницу
        </button>
      {/if}
      <button onclick={openCreate}
              class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-brand-700">
        + New invitation
      </button>
    </div>
  </div>

  <label class="flex items-center gap-2 text-sm text-slate-600">
    <input type="checkbox" bind:checked={includeUsed} onchange={refresh} />
    Show used & revoked
  </label>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if items.length === 0}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">
      No invitations yet
    </div>
  {:else}
    <div class="overflow-hidden rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-sm">
        <thead class="bg-slate-50 text-left text-xs uppercase tracking-wider text-slate-500">
          <tr>
            <th class="px-4 py-2">Token</th>
            <th class="px-4 py-2">Group</th>
            <th class="px-4 py-2">Email/Note</th>
            <th class="px-4 py-2">Created by</th>
            <th class="px-4 py-2">Expires</th>
            <th class="px-4 py-2 text-center">Status</th>
            <th class="px-4 py-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as inv (inv.id)}
            {@const st = statusOf(inv)}
            <tr>
              <td class="px-4 py-3 font-mono text-xs text-slate-600">{inv.token_prefix}…</td>
              <td class="px-4 py-3 text-slate-700">{inv.group?.name ?? '—'}</td>
              <td class="px-4 py-3 text-slate-600">
                {#if inv.email}<div>{inv.email}</div>{/if}
                {#if inv.note}<div class="text-xs text-slate-400">{inv.note}</div>{/if}
                {#if !inv.email && !inv.note}—{/if}
              </td>
              <td class="px-4 py-3 text-slate-600">{inv.created_by?.username ?? '—'}</td>
              <td class="px-4 py-3 text-xs text-slate-500">
                {new Date(inv.expires_at).toLocaleString()}
              </td>
              <td class="px-4 py-3 text-center">
                <span class="rounded-full px-2 py-0.5 text-[11px] font-medium uppercase {st.class}">{st.label}</span>
                {#if inv.used_by}
                  <div class="mt-1 text-xs text-slate-400"><ArrowRight size={14} class="inline-block align-text-bottom" /> @{inv.used_by.username}</div>
                {/if}
              </td>
              <td class="px-4 py-3 text-right whitespace-nowrap">
                {#if !inv.used_at && !inv.is_revoked}
                  <button onclick={() => revoke(inv)}
                          class="text-xs text-amber-700 hover:text-amber-900">Revoke</button>
                {/if}
                {#if isSuper}
                  <button onclick={() => remove(inv)}
                          class="ml-3 text-xs text-red-600 hover:text-red-800"
                          title="Полностью удалить из БД">
                    Delete
                  </button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<!-- Create modal -->
{#if createOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">New invitation</h2>
      <form onsubmit={handleCreate} class="mt-4 space-y-3">
        <div>
          <label for="inv_group" class="block text-sm font-medium text-slate-700">Group</label>
          <select id="inv_group" bind:value={newGroupId} disabled={!isSuper}
                  class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-50">
            {#if isSuper}
              <option value={null}>— no group —</option>
            {/if}
            {#each allGroups as g}
              <option value={g.id}>{g.name}</option>
            {/each}
          </select>
          {#if !isSuper}
            <p class="mt-1 text-xs text-slate-400">group_admin может приглашать только в свою группу.</p>
          {/if}
        </div>

        <div>
          <span class="block text-sm font-medium text-slate-700">Roles</span>
          <div class="mt-1 max-h-32 space-y-1 overflow-auto rounded border border-slate-200 p-2">
            {#each allRoles as r}
              <label class="flex items-center gap-2 text-sm">
                <input type="checkbox" value={r.id} checked={newRoleIds.includes(r.id)}
                       onchange={(e) => {
                         if (e.currentTarget.checked) newRoleIds = [...newRoleIds, r.id]
                         else newRoleIds = newRoleIds.filter((id) => id !== r.id)
                       }} />
                <span>{r.name}</span>
                {#if r.is_system}<span class="text-xs text-slate-400">system</span>{/if}
              </label>
            {/each}
          </div>
          <p class="mt-1 text-xs text-slate-400">Если пусто — будет назначена роль <code>user</code>.</p>
        </div>

        <div>
          <label for="inv_email" class="block text-sm font-medium text-slate-700">
            Email <span class="text-slate-400">(optional, для записи)</span>
          </label>
          <input id="inv_email" type="email" bind:value={newEmail}
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>

        <div>
          <label for="inv_note" class="block text-sm font-medium text-slate-700">Note</label>
          <input id="inv_note" type="text" bind:value={newNote} maxlength="200"
                 placeholder="e.g. invite for Bob"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>

        <div>
          <label for="inv_ttl" class="block text-sm font-medium text-slate-700">Expires in (hours)</label>
          <input id="inv_ttl" type="number" bind:value={newTtlHours} min="1" max="2160"
                 class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <p class="mt-1 text-xs text-slate-400">12 ч (default) · 24 = сутки · 168 = неделя · max ~90 дней.</p>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (createOpen = false)}
                  class="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Cancel
          </button>
          <button type="submit" disabled={createBusy}
                  class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
            {createBusy ? 'Creating…' : 'Create invitation'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- One-shot URL display -->
{#if createdInvite}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (createdInvite = null)}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="w-full max-w-xl rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900"><Check size={14} class="inline-block align-text-bottom" /> Invitation created</h2>
      <p class="mt-1 text-sm text-amber-700">
        <strong>Этот URL показывается один раз.</strong> Скопируй и отдай коллеге. Потом получить его снова нельзя.
      </p>

      <div class="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3">
        <div class="flex items-start gap-2">
          <code class="flex-1 break-all text-xs text-slate-800">{createdInvite.invite_url}</code>
          <button onclick={copyUrl}
                  class="shrink-0 rounded-md bg-brand-600 px-3 py-1 text-xs font-medium text-white hover:bg-brand-700">
            {#if copied}<Check size={14} class="inline-block align-text-bottom" /> Copied{:else}Copy{/if}
          </button>
        </div>
      </div>

      <dl class="mt-4 grid gap-1 text-xs text-slate-600">
        <div><dt class="inline font-medium">Group:</dt> {createdInvite.group?.name ?? '—'}</div>
        <div><dt class="inline font-medium">Expires:</dt> {new Date(createdInvite.expires_at).toLocaleString()}</div>
        {#if createdInvite.note}
          <div><dt class="inline font-medium">Note:</dt> {createdInvite.note}</div>
        {/if}
      </dl>

      <div class="mt-6 flex justify-end">
        <button type="button" onclick={() => (createdInvite = null)}
                class="rounded-md bg-slate-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-900">
          Got it
        </button>
      </div>
    </div>
  </div>
{/if}
