<script lang="ts">
  import { HelpCircle, Share2 } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { aiSettings, groups as groupsApi, users as usersApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { AiModel, AiProvider, AiShareInfo, AiShareRequest, PromptTemplate, User } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let providers = $state<AiProvider[]>([])
  let prompts = $state<PromptTemplate[]>([])
  let loading = $state(true)

  // Кто я (для UI шаринга). can_manage на каждом ресурсе приходит с бэка.
  let me = $derived($currentUser)
  let isSuper = $derived(me?.is_super_admin ?? false)
  let isGroupAdmin = $derived((me?.roles ?? []).some((r) => r.name === 'group_admin'))
  let myGroupId = $derived(me?.group?.id ?? null)

  // Справочники для пикеров шаринга (список уже отфильтрован бэком по правам).
  let allUsers = $state<User[]>([])
  let allGroups = $state<{ id: number; name: string }[]>([])
  const userName = (id: number) => allUsers.find((u) => u.id === id)?.username ?? `#${id}`
  const groupName = (id: number) => allGroups.find((g) => g.id === id)?.name ?? `#${id}`

  async function refresh() {
    loading = true
    try {
      const [ps, ts] = await Promise.all([aiSettings.listProviders(), aiSettings.listPrompts()])
      providers = ps
      prompts = ts
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  async function loadDirectories() {
    try {
      const [us, gs] = await Promise.all([
        usersApi.list({ limit: 500 }).catch(() => ({ items: [] as User[] })),
        groupsApi.list().catch(() => [] as { id: number; name: string }[]),
      ])
      allUsers = (us as { items: User[] }).items ?? []
      allGroups = (gs as { id: number; name: string }[]) ?? []
    } catch { /* пикеры просто будут пустыми */ }
  }
  onMount(() => { refresh(); loadDirectories() })

  // Бейдж видимости ресурса
  function visBadge(r: AiShareInfo): { text: string; cls: string } {
    if (r.shared_all) return { text: 'виден всем', cls: 'bg-emerald-100 text-emerald-700' }
    const parts: string[] = []
    if (r.shared_group_ids.length) parts.push(`групп ${r.shared_group_ids.length}`)
    if (r.shared_user_ids.length) parts.push(`юзеров ${r.shared_user_ids.length}`)
    if (parts.length) return { text: 'общий · ' + parts.join(', '), cls: 'bg-blue-100 text-blue-700' }
    return { text: 'приватный', cls: 'bg-slate-100 text-slate-600' }
  }
  const ownerLabel = (r: AiShareInfo) =>
    r.owner_user_id && r.owner_user_id === me?.id ? 'моё' : (r.owner_username ?? '—')

  // ─── Share modal ───
  let shareOpen = $state(false)
  let shareKind = $state<'provider' | 'prompt'>('provider')
  let shareTarget = $state<AiProvider | PromptTemplate | null>(null)
  let sh_all = $state(false)
  let sh_userIds = $state<number[]>([])
  let sh_groupIds = $state<number[]>([])
  let shareBusy = $state(false)

  function openShare(kind: 'provider' | 'prompt', r: AiProvider | PromptTemplate) {
    shareKind = kind; shareTarget = r
    sh_all = r.shared_all
    sh_userIds = [...r.shared_user_ids]
    sh_groupIds = [...r.shared_group_ids]
    shareOpen = true
  }
  function toggleId(arr: number[], id: number): number[] {
    return arr.includes(id) ? arr.filter((x) => x !== id) : [...arr, id]
  }
  const shareMyGroup = $derived(myGroupId != null && sh_groupIds.includes(myGroupId))
  function toggleMyGroup() {
    if (myGroupId == null) return
    sh_groupIds = toggleId(sh_groupIds, myGroupId)
  }
  async function submitShare() {
    if (!shareTarget) return
    // Собираем payload по правам роли: не отправляем то, что менять нельзя.
    const payload: AiShareRequest = {}
    if (isSuper) {
      payload.shared_all = sh_all
      payload.user_ids = sh_userIds
      payload.group_ids = sh_groupIds
    } else if (isGroupAdmin) {
      // только пользователи своей группы (те, что видны в пикере) — иначе бэк отобьёт 403
      payload.user_ids = sh_userIds.filter((id) => allUsers.some((u) => u.id === id))
      payload.group_ids = shareMyGroup && myGroupId != null ? [myGroupId] : []
    } else {
      // обычный юзер: только своя группа (для промптов)
      payload.group_ids = shareMyGroup && myGroupId != null ? [myGroupId] : []
    }
    shareBusy = true
    try {
      if (shareKind === 'provider') await aiSettings.shareProvider(shareTarget.id, payload)
      else await aiSettings.sharePrompt(shareTarget.id, payload)
      showToast('success', 'Доступ обновлён')
      shareOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      shareBusy = false
    }
  }
  // Кнопка Share видна: провайдер — super/group_admin; промпт — ещё и юзеру (своей группе).
  const canShare = (kind: 'provider' | 'prompt', r: AiShareInfo) =>
    r.can_manage && (isSuper || isGroupAdmin || (kind === 'prompt' && myGroupId != null))

  function typeBadge(t: string): string {
    switch (t) {
      case 'openai': return 'bg-emerald-100 text-emerald-700'
      case 'anthropic': return 'bg-amber-100 text-amber-700'
      case 'google': return 'bg-blue-100 text-blue-700'
      default: return 'bg-slate-100 text-slate-600'
    }
  }
  function purposeBadge(p: string): string {
    switch (p) {
      case 'content': return 'bg-indigo-100 text-indigo-700'
      case 'spin': return 'bg-purple-100 text-purple-700'
      default: return 'bg-slate-100 text-slate-600'
    }
  }

  // ─── Provider modal ───
  let provHelpOpen = $state(false)   // справка по провайдерам/моделям
  let provOpen = $state(false)
  let provEdit = $state<AiProvider | null>(null)
  let p_name = $state(''), p_type = $state('openai'), p_key = $state('')
  let p_base = $state(''), p_active = $state(true)

  function openProvCreate() {
    provEdit = null; p_name = ''; p_type = 'openai'; p_key = ''; p_base = ''; p_active = true
    provOpen = true
  }
  function openProvEdit(p: AiProvider) {
    provEdit = p; p_name = p.name; p_type = p.type; p_key = ''
    p_base = p.base_url ?? ''; p_active = p.is_active; provOpen = true
  }
  async function submitProv(e: SubmitEvent) {
    e.preventDefault()
    try {
      if (provEdit) {
        await aiSettings.updateProvider(provEdit.id, {
          name: p_name, type: p_type, base_url: p_base || null, is_active: p_active,
          ...(p_key ? { api_key: p_key } : {}),
        })
        showToast('success', 'Провайдер обновлён')
      } else {
        await aiSettings.createProvider({ name: p_name, type: p_type, api_key: p_key, base_url: p_base || null, is_active: p_active })
        showToast('success', 'Провайдер добавлен')
      }
      provOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
  async function delProvider(p: AiProvider) {
    if (!confirm(`Удалить провайдера «${p.name}» и все его модели?`)) return
    try { await aiSettings.deleteProvider(p.id); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }

  // ─── Model modal ───
  let modelOpen = $state(false)
  let modelEdit = $state<AiModel | null>(null)
  let m_provider = $state<number | null>(null)
  let m_display = $state(''), m_modelid = $state('')
  let m_temp = $state(0.7), m_maxtok = $state(4096)
  let m_purpose = $state('content'), m_active = $state(true)

  function openModelCreate(providerId: number) {
    modelEdit = null; m_provider = providerId; m_display = ''; m_modelid = ''
    m_temp = 0.7; m_maxtok = 4096; m_purpose = 'content'; m_active = true; modelOpen = true
  }
  function openModelEdit(m: AiModel) {
    modelEdit = m; m_provider = m.provider_id; m_display = m.display_name; m_modelid = m.model_id
    m_temp = m.temperature; m_maxtok = m.max_tokens; m_purpose = m.purpose; m_active = m.is_active
    modelOpen = true
  }
  async function submitModel(e: SubmitEvent) {
    e.preventDefault()
    try {
      if (modelEdit) {
        await aiSettings.updateModel(modelEdit.id, {
          display_name: m_display, model_id: m_modelid, temperature: m_temp,
          max_tokens: m_maxtok, purpose: m_purpose, is_active: m_active,
        })
      } else {
        await aiSettings.createModel({
          provider_id: m_provider!, display_name: m_display, model_id: m_modelid,
          temperature: m_temp, max_tokens: m_maxtok, purpose: m_purpose, is_active: m_active,
        })
      }
      modelOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
  async function delModel(m: AiModel) {
    if (!confirm(`Удалить модель «${m.display_name}»?`)) return
    try { await aiSettings.deleteModel(m.id); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }

  // ─── Prompt modal ───
  let promptOpen = $state(false)
  let promptHelpOpen = $state(false)   // справка по оформлению промпта
  let promptEdit = $state<PromptTemplate | null>(null)
  let t_name = $state(''), t_body = $state(''), t_notes = $state('')

  function openPromptCreate() {
    promptEdit = null; t_name = ''; t_body = ''; t_notes = ''; promptOpen = true
  }
  function openPromptEdit(t: PromptTemplate) {
    promptEdit = t; t_name = t.name; t_body = t.body; t_notes = t.notes ?? ''; promptOpen = true
  }
  async function submitPrompt(e: SubmitEvent) {
    e.preventDefault()
    try {
      if (promptEdit) await aiSettings.updatePrompt(promptEdit.id, { name: t_name, body: t_body, notes: t_notes || null })
      else await aiSettings.createPrompt({ name: t_name, body: t_body, notes: t_notes || null })
      promptOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }
  async function delPrompt(t: PromptTemplate) {
    if (!confirm(`Удалить шаблон «${t.name}»?`)) return
    try { await aiSettings.deletePrompt(t.id); await refresh() }
    catch (e) { showToast('error', e instanceof ApiError ? e.message : String(e)) }
  }
</script>

<div class="mx-auto max-w-5xl p-6">
  <div class="flex items-center justify-between">
    <div>
      <h1 class="text-xl font-semibold text-slate-900">AI Settings</h1>
      <p class="mt-1 text-sm text-slate-500">Провайдеры, модели и шаблоны промптов для генерации/спина контента.</p>
    </div>
  </div>

  {#if loading}
    <p class="mt-8 text-sm text-slate-500">Загрузка…</p>
  {:else}
    <!-- ─── Providers ─── -->
    <section class="mt-6">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Провайдеры и модели</h2>
        <div class="flex items-center gap-2">
          <button type="button" onclick={() => (provHelpOpen = true)}
                  title="Что такое провайдер, модель и назначение"
                  aria-label="Справка по провайдерам и моделям"
                  class="inline-flex items-center justify-center rounded-md border border-slate-300 p-1.5 text-slate-600 hover:bg-slate-50">
            <HelpCircle size={18} />
          </button>
          <button onclick={openProvCreate} class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">+ Провайдер</button>
        </div>
      </div>

      {#if providers.length === 0}
        <p class="mt-3 rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-500">Нет провайдеров. Добавь ключ от OpenAI / Anthropic / Google.</p>
      {/if}

      <div class="mt-3 space-y-3">
        {#each providers as p (p.id)}
          <div class="rounded-lg border border-slate-200 bg-white">
            <div class="flex items-center justify-between border-b border-slate-100 px-4 py-3">
              <div class="flex min-w-0 flex-wrap items-center gap-2">
                <span class="font-medium text-slate-900">{p.name}</span>
                <span class="rounded px-2 py-0.5 text-xs font-medium {typeBadge(p.type)}">{p.type}</span>
                {#if !p.is_active}<span class="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-500">disabled</span>{/if}
                {#if p.has_key}<span class="rounded bg-emerald-50 px-2 py-0.5 text-xs text-emerald-600">🔑 key set</span>{/if}
                <span class="rounded px-2 py-0.5 text-xs font-medium {visBadge(p).cls}">{visBadge(p).text}</span>
                <span class="text-xs text-slate-400">владелец: {ownerLabel(p)}</span>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                {#if canShare('provider', p)}
                  <button onclick={() => openShare('provider', p)} title="Поделиться ключом"
                          class="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2.5 py-1 text-xs hover:bg-slate-50">
                    <Share2 size={13} /> Share
                  </button>
                {/if}
                {#if p.can_manage}
                  <button onclick={() => openModelCreate(p.id)} class="rounded-md border border-slate-300 px-2.5 py-1 text-xs">+ Модель</button>
                  <button onclick={() => openProvEdit(p)} class="rounded-md border border-slate-300 px-2.5 py-1 text-xs">Изм.</button>
                  <button onclick={() => delProvider(p)} class="rounded-md border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Удал.</button>
                {/if}
              </div>
            </div>
            {#if p.models.length === 0}
              <p class="px-4 py-2 text-xs text-slate-400">Нет моделей</p>
            {:else}
              <table class="w-full text-sm">
                <tbody>
                  {#each p.models as m (m.id)}
                    <tr class="border-b border-slate-50 last:border-0">
                      <td class="px-4 py-2">
                        <span class="font-medium text-slate-800">{m.display_name}</span>
                        <span class="ml-2 font-mono text-xs text-slate-500">{m.model_id}</span>
                      </td>
                      <td class="px-2 py-2"><span class="rounded px-2 py-0.5 text-xs font-medium {purposeBadge(m.purpose)}">{m.purpose}</span></td>
                      <td class="px-2 py-2 text-xs text-slate-500">t={m.temperature} · {m.max_tokens}tok</td>
                      <td class="px-2 py-2 text-xs">{#if !m.is_active}<span class="text-slate-400">disabled</span>{/if}</td>
                      <td class="px-4 py-2 text-right">
                        {#if p.can_manage}
                          <button onclick={() => openModelEdit(m)} class="text-xs text-brand-600 hover:underline">изм.</button>
                          <button onclick={() => delModel(m)} class="ml-2 text-xs text-red-600 hover:underline">удал.</button>
                        {/if}
                      </td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {/if}
          </div>
        {/each}
      </div>
    </section>

    <!-- ─── Prompt templates ─── -->
    <section class="mt-8">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Шаблоны промптов</h2>
        <div class="flex items-center gap-2">
          <button type="button" onclick={() => (promptHelpOpen = true)}
                  title="Как оформить промпт"
                  aria-label="Как оформить промпт"
                  class="inline-flex items-center justify-center rounded-md border border-slate-300 p-1.5 text-slate-600 hover:bg-slate-50">
            <HelpCircle size={18} />
          </button>
          <button onclick={openPromptCreate} class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">+ Шаблон</button>
        </div>
      </div>
      <p class="mt-1 text-xs text-slate-400">Плейсхолдеры: <code>{'{keyword}'}</code>, <code>{'{anchor}'}</code>, <code>{'{link}'}</code>, <code>{'{language}'}</code> + поля из content_parametrs.</p>

      {#if prompts.length === 0}
        <p class="mt-3 rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-500">Нет шаблонов.</p>
      {:else}
        <div class="mt-3 space-y-2">
          {#each prompts as t (t.id)}
            <div class="rounded-lg border border-slate-200 bg-white px-4 py-3">
              <div class="flex items-start justify-between">
                <div class="min-w-0">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="font-medium text-slate-900">{t.name}</span>
                    <span class="rounded px-2 py-0.5 text-xs font-medium {visBadge(t).cls}">{visBadge(t).text}</span>
                    <span class="text-xs text-slate-400">владелец: {ownerLabel(t)}</span>
                    {#if t.notes}<span class="text-xs text-slate-400">· {t.notes}</span>{/if}
                  </div>
                  <p class="mt-1 line-clamp-2 whitespace-pre-wrap font-mono text-xs text-slate-500">{t.body}</p>
                </div>
                <div class="ml-3 flex shrink-0 gap-2">
                  {#if canShare('prompt', t)}
                    <button onclick={() => openShare('prompt', t)} title="Поделиться промптом"
                            class="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2.5 py-1 text-xs hover:bg-slate-50">
                      <Share2 size={13} /> Share
                    </button>
                  {/if}
                  {#if t.can_manage}
                    <button onclick={() => openPromptEdit(t)} class="rounded-md border border-slate-300 px-2.5 py-1 text-xs">Изм.</button>
                    <button onclick={() => delPrompt(t)} class="rounded-md border border-red-200 px-2.5 py-1 text-xs text-red-600 hover:bg-red-50">Удал.</button>
                  {/if}
                </div>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </section>
  {/if}
</div>

<!-- ─── Share modal ─── -->
{#if shareOpen && shareTarget}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (shareOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">Доступ: {shareTarget.name}</h2>
      <p class="mt-1 text-xs text-slate-500">{shareKind === 'provider' ? 'Кому открыт этот ключ.' : 'Кому открыт этот промпт.'}</p>
      <div class="mt-4 space-y-4">
        {#if isSuper}
          <label class="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" bind:checked={sh_all} /> Виден всем (дефолт для всех пользователей)
          </label>
          {#if !sh_all}
            <div>
              <p class="text-xs font-medium text-slate-700">Группы</p>
              <div class="mt-1 max-h-32 overflow-auto rounded-md border border-slate-200 p-2">
                {#each allGroups as g (g.id)}
                  <label class="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={sh_groupIds.includes(g.id)} onchange={() => (sh_groupIds = toggleId(sh_groupIds, g.id))} /> {g.name}
                  </label>
                {:else}
                  <p class="text-xs text-slate-400">Групп нет</p>
                {/each}
              </div>
            </div>
            <div>
              <p class="text-xs font-medium text-slate-700">Пользователи</p>
              <div class="mt-1 max-h-32 overflow-auto rounded-md border border-slate-200 p-2">
                {#each allUsers as u (u.id)}
                  <label class="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={sh_userIds.includes(u.id)} onchange={() => (sh_userIds = toggleId(sh_userIds, u.id))} /> {u.username}
                  </label>
                {:else}
                  <p class="text-xs text-slate-400">Пользователей нет</p>
                {/each}
              </div>
            </div>
          {/if}
        {:else if isGroupAdmin}
          {#if myGroupId != null}
            <label class="flex items-center gap-2 text-sm text-slate-700">
              <input type="checkbox" checked={shareMyGroup} onchange={toggleMyGroup} /> Открыть всей моей группе
            </label>
          {/if}
          <div>
            <p class="text-xs font-medium text-slate-700">Пользователи моей группы</p>
            <div class="mt-1 max-h-40 overflow-auto rounded-md border border-slate-200 p-2">
              {#each allUsers as u (u.id)}
                <label class="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={sh_userIds.includes(u.id)} onchange={() => (sh_userIds = toggleId(sh_userIds, u.id))} /> {u.username}
                </label>
              {:else}
                <p class="text-xs text-slate-400">Нет пользователей</p>
              {/each}
            </div>
          </div>
        {:else if myGroupId != null}
          <label class="flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={shareMyGroup} onchange={toggleMyGroup} /> Открыть моей группе
          </label>
        {:else}
          <p class="text-sm text-slate-500">Вы не состоите в группе — шарить некому.</p>
        {/if}
      </div>
      <div class="flex justify-end gap-2 pt-4">
        <button type="button" onclick={() => (shareOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
        <button type="button" onclick={submitShare} disabled={shareBusy}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:opacity-50">Сохранить</button>
      </div>
    </div>
  </div>
{/if}

<!-- ─── Provider modal ─── -->
{#if provOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (provOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">{provEdit ? 'Изменить провайдера' : 'Новый провайдер'}</h2>
      <form onsubmit={submitProv} class="mt-4 space-y-3">
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label for="pv_name" class="block text-xs font-medium text-slate-700">Название *</label>
            <input id="pv_name" bind:value={p_name} required class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
          <div>
            <label for="pv_type" class="block text-xs font-medium text-slate-700">Тип *</label>
            <select id="pv_type" bind:value={p_type} class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
              <option value="google">google</option>
            </select>
          </div>
        </div>
        <div>
          <label for="pv_key" class="block text-xs font-medium text-slate-700">API key {provEdit ? '(пусто = не менять)' : '*'}</label>
          <input id="pv_key" type="password" bind:value={p_key} required={!provEdit} autocomplete="off"
                 class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm font-mono" />
        </div>
        <div>
          <label for="pv_base" class="block text-xs font-medium text-slate-700">Base URL (optional)</label>
          <input id="pv_base" bind:value={p_base} placeholder="https://api.openai.com/v1" class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm font-mono" />
        </div>
        <label class="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" bind:checked={p_active} /> Активен</label>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (provOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">{provEdit ? 'Сохранить' : 'Добавить'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- ─── Model modal ─── -->
{#if modelOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (modelOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-md rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">{modelEdit ? 'Изменить модель' : 'Новая модель'}</h2>
      <form onsubmit={submitModel} class="mt-4 space-y-3">
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label for="md_disp" class="block text-xs font-medium text-slate-700">Название *</label>
            <input id="md_disp" bind:value={m_display} required class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
          <div>
            <label for="md_mid" class="block text-xs font-medium text-slate-700">model_id (API) *</label>
            <input id="md_mid" bind:value={m_modelid} required placeholder="gpt-4o-mini" class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm font-mono" />
          </div>
        </div>
        <div class="grid grid-cols-3 gap-2">
          <div>
            <label for="md_purp" class="block text-xs font-medium text-slate-700">Назначение</label>
            <select id="md_purp" bind:value={m_purpose} class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="content">content</option>
              <option value="spin">spin</option>
              <option value="any">any</option>
            </select>
          </div>
          <div>
            <label for="md_temp" class="block text-xs font-medium text-slate-700">Temp</label>
            <input id="md_temp" type="number" step="0.1" min="0" max="2" bind:value={m_temp} class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
          <div>
            <label for="md_tok" class="block text-xs font-medium text-slate-700">Max tokens</label>
            <input id="md_tok" type="number" min="1" bind:value={m_maxtok} class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>
        </div>
        <label class="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" bind:checked={m_active} /> Активна</label>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (modelOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">{modelEdit ? 'Сохранить' : 'Добавить'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- ─── Prompt modal ─── -->
{#if promptOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (promptOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <h2 class="text-lg font-semibold text-slate-900">{promptEdit ? 'Изменить шаблон' : 'Новый шаблон промпта'}</h2>
      <form onsubmit={submitPrompt} class="mt-4 space-y-3">
        <div>
          <label for="pt_name" class="block text-xs font-medium text-slate-700">Название *</label>
          <input id="pt_name" bind:value={t_name} required class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>
        <div>
          <label for="pt_body" class="block text-xs font-medium text-slate-700">Тело промпта *</label>
          <textarea id="pt_body" bind:value={t_body} required rows="10"
                    class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-sm"></textarea>
        </div>
        <div>
          <label for="pt_notes" class="block text-xs font-medium text-slate-700">Заметка (optional)</label>
          <input id="pt_notes" bind:value={t_notes} class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" onclick={() => (promptOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm">Cancel</button>
          <button type="submit" class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">{promptEdit ? 'Сохранить' : 'Добавить'}</button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Help: провайдеры и модели -->
{#if provHelpOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (provHelpOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg bg-white shadow-xl" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-start justify-between border-b border-slate-100 px-6 py-4">
        <h2 class="text-lg font-semibold text-slate-900">Провайдеры и модели</h2>
        <button type="button" onclick={() => (provHelpOpen = false)} class="text-slate-400 hover:text-slate-700">✕</button>
      </div>

      <div class="space-y-4 overflow-auto px-6 py-5 text-sm text-slate-700">
        <p class="text-slate-600">
          Здесь подключаются AI для генерации и спина текстов. Иерархия: <b>провайдер</b> (доступ по API-ключу)
          → внутри него <b>модели</b> → у каждой модели — <b>назначение</b>.
        </p>

        <section>
          <h3 class="font-semibold text-slate-900">Провайдер</h3>
          <p class="mt-1.5 text-slate-600">
            Подключение к AI-сервису: <b>openai / anthropic / google</b> + <b>API-ключ</b> (хранится зашифрованным,
            <span class="rounded bg-emerald-50 px-1 text-emerald-600">🔑 key set</span> = ключ задан). Можно несколько
            провайдеров. <b>Base URL</b> — опц., для своего эндпоинта / совместимого API. Неактивный провайдер не используется.
          </p>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">Модель</h3>
          <p class="mt-1.5 text-slate-600">Конкретная модель внутри провайдера. Поля:</p>
          <ul class="mt-1.5 space-y-1 text-slate-600">
            <li><b>model_id</b> — точный идентификатор для API (напр. <code class="rounded bg-slate-100 px-1">gpt-4o-mini</code>); название — любое, для себя.</li>
            <li><b>Temp</b> — «креативность» (0 = строго, выше = разнообразнее).</li>
            <li><b>Max tokens</b> — потолок длины ответа.</li>
          </ul>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">Назначение (purpose)</h3>
          <ul class="mt-1.5 space-y-1 text-slate-600">
            <li><span class="rounded bg-indigo-100 px-1.5 py-0.5 text-[11px] font-medium text-indigo-700">content</span> — пишет тексты постов (основная генерация).</li>
            <li><span class="rounded bg-purple-100 px-1.5 py-0.5 text-[11px] font-medium text-purple-700">spin</span> — делает спинтакс/переспин (расшивка «текст на строку» в варианты).</li>
            <li><span class="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-600">any</span> — годится и туда, и туда.</li>
          </ul>
          <p class="mt-2 text-[12px] text-slate-500">
            При создании задачи (New run → CSV с генерацией) выбираешь content-модель; spin-модель
            движок берёт автоматически из активных.
          </p>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">Кнопки</h3>
          <p class="mt-1.5 text-slate-600">
            <b>+ Провайдер</b> / <b>+ Модель</b> — добавить · <b>Изм.</b> — редактировать · <b>Удал.</b> — удалить
            (провайдер удаляется со всеми моделями). <code>disabled</code> = выключено, в работу не идёт.
          </p>
        </section>
      </div>

      <div class="flex justify-end border-t border-slate-100 px-6 py-4">
        <button type="button" onclick={() => (provHelpOpen = false)}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Понятно</button>
      </div>
    </div>
  </div>
{/if}

<!-- Help: правила оформления промпта -->
{#if promptHelpOpen}
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="fixed inset-0 z-30 flex items-center justify-center bg-slate-900/40 p-4" onclick={() => (promptHelpOpen = false)}>
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-6 shadow-xl" onclick={(e) => e.stopPropagation()}>
      <div class="flex items-start justify-between">
        <h2 class="text-lg font-semibold text-slate-900">Как оформить промпт</h2>
        <button type="button" onclick={() => (promptHelpOpen = false)} class="text-slate-400 hover:text-slate-700">✕</button>
      </div>

      <div class="mt-4 space-y-5 text-sm text-slate-700">
        <section>
          <h3 class="font-semibold text-slate-900">1. Переменные (плейсхолдеры)</h3>
          <p class="mt-1 text-slate-600">В тексте промпта подставляются значения из файла задачи. Используй такие переменные:</p>
          <div class="mt-2 overflow-auto rounded border border-slate-200">
            <table class="min-w-full text-[13px]">
              <tbody class="divide-y divide-slate-100">
                <tr><td class="px-3 py-1.5 font-medium text-slate-800">BRAND_NAME</td><td class="px-3 py-1.5"><code class="rounded bg-slate-100 px-1">{'{keyword}'}</code></td><td class="px-3 py-1.5 text-slate-500">столбец keyword</td></tr>
                <tr><td class="px-3 py-1.5 font-medium text-slate-800">LANGUAGE</td><td class="px-3 py-1.5"><code class="rounded bg-slate-100 px-1">{'{language}'}</code></td><td class="px-3 py-1.5 text-slate-500">столбец language / язык формы</td></tr>
                <tr><td class="px-3 py-1.5 font-medium text-slate-800">LINK_ANCHOR</td><td class="px-3 py-1.5"><code class="rounded bg-slate-100 px-1">{'{anchor}'}</code></td><td class="px-3 py-1.5 text-slate-500">столбец anchor</td></tr>
                <tr><td class="px-3 py-1.5 font-medium text-slate-800">LINK_URL</td><td class="px-3 py-1.5"><code class="rounded bg-slate-100 px-1">{'{links}'}</code> <span class="text-slate-400">(или {'{link}'})</span></td><td class="px-3 py-1.5 text-slate-500">столбец link</td></tr>
                <tr><td class="px-3 py-1.5 font-medium text-slate-800">WORD_COUNT</td><td class="px-3 py-1.5 text-slate-500" colspan="2">задаёшь текстом, напр. «Aim for 800–1000 words.»</td></tr>
              </tbody>
            </table>
          </div>
          <p class="mt-2 text-[12px] text-slate-500">Любое поле из <code>content_parametrs</code> (JSON в файле) тоже доступно как <code>{'{имя_поля}'}</code>. Неизвестные плейсхолдеры остаются как есть.</p>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">2. Блок INPUTS (рекомендуется)</h3>
          <p class="mt-1 text-slate-600">Передавай модели вводные явным блоком — так она точно подставит ссылку/анкор/бренд:</p>
          <pre class="mt-2 overflow-auto rounded bg-slate-50 p-3 text-[12px] text-slate-700">INPUTS:
- BRAND_NAME: {'{keyword}'}
- WORD_COUNT: Aim for 800-1000 words.
- LANGUAGE: {'{language}'}
- LINK_ANCHOR: {'{anchor}'}
- LINK_URL: {'{links}'}</pre>
        </section>

        <section>
          <h3 class="font-semibold text-slate-900">3. Формат вывода (обязательно в конце)</h3>
          <p class="mt-1 text-slate-600">В самом конце промпта укажи формат — мы парсим <code>&lt;title&gt;</code> в заголовок, а содержимое <code>&lt;text&gt;</code> — в тело поста (теги в тело не попадают):</p>
          <pre class="mt-2 overflow-auto rounded bg-slate-50 p-3 text-[12px] text-slate-700">OUTPUT FORMAT:
&lt;title&gt;[Insert SEO Meta Title Here]&lt;/title&gt;
&lt;text&gt;
[Insert Full HTML Article Here]
&lt;/text&gt;</pre>
          <p class="mt-2 text-[12px] text-slate-500">Если этот блок не указать — заголовок будет пустым, а служебные теги могут попасть в текст. Markdown-обёртку <code>```html</code> снимаем автоматически.</p>
        </section>
      </div>

      <div class="mt-5 flex justify-end">
        <button type="button" onclick={() => (promptHelpOpen = false)}
                class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">Понятно</button>
      </div>
    </div>
  </div>
{/if}
