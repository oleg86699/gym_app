<script lang="ts">
  import { onMount } from 'svelte'
  import { Check, Copy, Eye, EyeOff } from 'lucide-svelte'
  import { supplierAccess, wpBatches, type SupplierAccessCreated, type SupplierAccessItem } from '$lib/api/admin'
  import type { WpBatch } from '$lib/api/types'
  import { ApiError } from '$lib/api/client'
  import { copyText } from '$lib/clipboard'
  import { showToast } from '$lib/stores/toast'

  let items = $state<SupplierAccessItem[]>([])
  let loading = $state(true)
  let revealed = $state<Set<number>>(new Set())

  function toggleReveal(id: number) {
    if (revealed.has(id)) revealed.delete(id)
    else revealed.add(id)
    revealed = new Set(revealed)
  }

  let createOpen = $state(false)
  let busy = $state(false)
  let note = $state('')
  let ttlHours = $state(24 * 7)
  let handover = $state<'password' | 'link'>('password')

  // Батчи, к которым сразу открыть доступ поставщику (15 последних, chip-toggle).
  let availableBatches = $state<WpBatch[]>([])
  let selectedBatchIds = $state<number[]>([])
  let batchesLoading = $state(false)
  function toggleBatch(id: number) {
    selectedBatchIds = selectedBatchIds.includes(id)
      ? selectedBatchIds.filter((b) => b !== id)
      : [...selectedBatchIds, id]
  }

  let created = $state<SupplierAccessCreated | null>(null)
  let copied = $state<string | null>(null)
  let regenBusy = $state<number | null>(null)

  async function refresh() {
    loading = true
    try {
      items = (await supplierAccess.list()).items
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }
  onMount(refresh)

  async function openCreate() {
    note = ''; ttlHours = 24 * 7; handover = 'password'
    selectedBatchIds = []
    createOpen = true
    batchesLoading = true
    try {
      availableBatches = (await wpBatches.list({ limit: 15 })).items
    } catch {
      availableBatches = []
    } finally {
      batchesLoading = false
    }
  }

  async function create(e: SubmitEvent) {
    e.preventDefault()
    busy = true
    try {
      created = await supplierAccess.create({
        note: note || undefined,
        ttl_hours: ttlHours,
        handover,
        batch_ids: selectedBatchIds.length ? selectedBatchIds : undefined,
      })
      createOpen = false
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? String(e.detail ?? e.message) : String(e))
    } finally {
      busy = false
    }
  }

  async function revoke(userId: number) {
    if (!confirm('Отозвать доступ? Поставщик больше не сможет войти.')) return
    try {
      await supplierAccess.revoke(userId)
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    }
  }

  async function regenerateLink(userId: number) {
    if (!confirm('Сгенерировать НОВУЮ ссылку? Старая перестанет работать.')) return
    regenBusy = userId
    try {
      const res = await supplierAccess.regenerateLink(userId)
      await copyText(res.magic_url)
      showToast('success', 'Новая ссылка скопирована в буфер')
      await refresh()
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      regenBusy = null
    }
  }

  async function copy(text: string, key: string) {
    if (await copyText(text)) {
      copied = key
      setTimeout(() => (copied = null), 1500)
    }
  }

  function fmt(d: string | null): string {
    return d ? new Date(d).toLocaleString() : '—'
  }
  function statusLabel(it: SupplierAccessItem): { text: string; cls: string } {
    if (!it.is_active) return { text: 'отозван', cls: 'bg-slate-200 text-slate-500' }
    if (it.is_expired) return { text: 'истёк', cls: 'bg-amber-100 text-amber-700' }
    return { text: 'активен', cls: 'bg-green-100 text-green-700' }
  }
</script>

<div class="rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
  <div class="flex items-center justify-between">
    <div>
      <h2 class="text-base font-semibold text-slate-900">Доступы поставщиков</h2>
      <p class="mt-0.5 text-sm text-slate-500">
        Временный вход для того, кто даёт доступы — он сам грузит файл и видит наши цифры валидации. Только его файлы.
      </p>
    </div>
    <button onclick={openCreate} class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700">
      Создать доступ
    </button>
  </div>

  {#if loading}
    <div class="py-6 text-center text-sm text-slate-400">Загрузка…</div>
  {:else if items.length === 0}
    <div class="py-6 text-center text-sm text-slate-400">Пока нет доступов поставщиков</div>
  {:else}
    <div class="mt-4 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="border-b border-slate-200 text-left text-xs uppercase tracking-wider text-slate-400">
          <tr>
            <th class="py-2 pr-3">Логин</th>
            <th class="py-2 pr-3">Пароль</th>
            <th class="py-2 pr-3">Заметка</th>
            <th class="py-2 pr-3">Передача</th>
            <th class="py-2 pr-3">Статус</th>
            <th class="py-2 pr-3">Истекает</th>
            <th class="py-2 pr-3">Вход</th>
            <th class="py-2"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          {#each items as it}
            {@const st = statusLabel(it)}
            <tr>
              <td class="py-2 pr-3 font-mono text-xs text-slate-700">{it.username}</td>
              <td class="py-2 pr-3">
                {#if it.password}
                  {@const shown = revealed.has(it.user_id)}
                  <div class="flex items-center gap-1.5">
                    <code class="font-mono text-xs text-slate-700">{shown ? it.password : '••••••••'}</code>
                    <button type="button" onclick={() => toggleReveal(it.user_id)}
                            class="text-slate-400 hover:text-slate-600" title={shown ? 'скрыть' : 'показать'}>
                      {#if shown}<EyeOff size={13} />{:else}<Eye size={13} />{/if}
                    </button>
                    <button type="button" onclick={() => copy(it.password ?? '', `pw-${it.user_id}`)}
                            class="text-brand-600 hover:text-brand-700" title="копировать">
                      {#if copied === `pw-${it.user_id}`}<Check size={13} />{:else}<Copy size={13} />{/if}
                    </button>
                  </div>
                {:else}
                  <span class="text-slate-300" title="пароль не сохранён (старый аккаунт или magic-link)">—</span>
                {/if}
              </td>
              <td class="py-2 pr-3 text-slate-600">{it.note ?? '—'}</td>
              <td class="py-2 pr-3 text-slate-500">
                {#if it.handover === 'link'}
                  <div class="flex items-center gap-1.5">
                    <span>ссылка</span>
                    {#if it.magic_url}
                      <button type="button" onclick={() => copy(it.magic_url ?? '', `ml-${it.user_id}`)}
                              class="text-brand-600 hover:text-brand-700" title="копировать ссылку">
                        {#if copied === `ml-${it.user_id}`}<Check size={13} />{:else}<Copy size={13} />{/if}
                      </button>
                    {/if}
                    {#if it.is_active && !it.is_expired}
                      <button type="button" onclick={() => regenerateLink(it.user_id)} disabled={regenBusy === it.user_id}
                              class="text-[11px] text-slate-400 hover:text-slate-600 disabled:opacity-50"
                              title={it.magic_url ? 'Новая ссылка (старая перестанет работать)' : 'Получить ссылку (старый доступ — исходная невосстановима)'}>
                        {regenBusy === it.user_id ? '…' : 'обновить'}
                      </button>
                    {/if}
                  </div>
                {:else}
                  логин+пароль
                {/if}
              </td>
              <td class="py-2 pr-3"><span class="rounded px-1.5 py-0.5 text-[11px] font-medium {st.cls}">{st.text}</span></td>
              <td class="py-2 pr-3 text-slate-500">{fmt(it.expires_at)}</td>
              <td class="py-2 pr-3 text-slate-500">{it.last_login_at ? fmt(it.last_login_at) : 'не входил'}</td>
              <td class="py-2 text-right">
                {#if it.is_active}
                  <button onclick={() => revoke(it.user_id)} class="text-xs text-red-600 hover:underline">Отозвать</button>
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
  <div class="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
    <form onsubmit={create} class="w-full max-w-md space-y-4 rounded-lg bg-white p-6 shadow-xl">
      <h3 class="text-lg font-semibold text-slate-900">Доступ поставщика</h3>
      <div>
        <label for="sa-note" class="block text-sm font-medium text-slate-700">Заметка (кому)</label>
        <input id="sa-note" bind:value={note} maxlength="200" placeholder="например, Поставщик Иван"
          class="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none" />
      </div>
      <div>
        <label for="sa-ttl" class="block text-sm font-medium text-slate-700">Срок действия (часов)</label>
        <input id="sa-ttl" type="number" bind:value={ttlHours} min="1" max={24 * 90}
          class="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none" />
        <p class="mt-1 text-xs text-slate-400">168 = 7 дней (по умолчанию), 24 = сутки, макс ~90 дней</p>
      </div>
      <div>
        <span class="block text-sm font-medium text-slate-700">Как передать доступ</span>
        <div class="mt-2 space-y-2">
          <label class="flex items-start gap-2 text-sm">
            <input type="radio" bind:group={handover} value="password" class="mt-0.5" />
            <span><b>Логин + пароль</b> — сгенерируем учётку, передашь поставщику (вход на /login)</span>
          </label>
          <label class="flex items-start gap-2 text-sm">
            <input type="radio" bind:group={handover} value="link" class="mt-0.5" />
            <span><b>Magic-ссылка</b> — одна ссылка, по ней входит без пароля</span>
          </label>
        </div>
      </div>

      <!-- Батчи, к которым сразу открыть доступ поставщику -->
      <div>
        <span class="block text-sm font-medium text-slate-700">Открыть доступ к батчам</span>
        <p class="mt-0.5 text-xs text-slate-400">
          Необязательно. Поставщик сразу увидит результаты выбранных файлов при входе. Показаны 15 последних.
        </p>
        {#if batchesLoading}
          <div class="mt-2 text-xs text-slate-400">Загрузка…</div>
        {:else if availableBatches.length === 0}
          <div class="mt-2 text-xs text-slate-400">Батчей пока нет.</div>
        {:else}
          <div class="mt-2 max-h-44 space-y-1 overflow-auto rounded-md border border-slate-200 p-1.5">
            {#each availableBatches as b}
              {@const sel = selectedBatchIds.includes(b.id)}
              <button type="button" onclick={() => toggleBatch(b.id)}
                      class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left {sel ? 'bg-brand-50 ring-1 ring-brand-300' : 'hover:bg-slate-50'}">
                <span class="flex h-4 w-4 shrink-0 items-center justify-center rounded border {sel ? 'border-brand-500 bg-brand-500 text-white' : 'border-slate-300'}">
                  {#if sel}<Check size={11} />{/if}
                </span>
                <span class="min-w-0 flex-1 truncate text-sm">
                  <span class="font-medium text-slate-800">{b.name}</span>
                  {#if b.tag}<span class="ml-1 text-[11px] text-slate-400">#{b.tag}</span>{/if}
                </span>
                <span class="shrink-0 text-[11px] text-slate-400">{b.valid_count}/{b.total_credentials}</span>
              </button>
            {/each}
          </div>
          {#if selectedBatchIds.length}
            <p class="mt-1 text-xs text-brand-600">Выбрано: <b>{selectedBatchIds.length}</b> — откроются поставщику сразу.</p>
          {/if}
        {/if}
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <button type="button" onclick={() => (createOpen = false)} class="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50">Отмена</button>
        <button type="submit" disabled={busy} class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
          {busy ? 'Создаём…' : 'Создать'}
        </button>
      </div>
    </form>
  </div>
{/if}

<!-- Result modal (one-shot) -->
{#if created}
  <div class="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
    <div class="w-full max-w-lg space-y-4 rounded-lg bg-white p-6 shadow-xl">
      <h3 class="text-lg font-semibold text-slate-900">Доступ создан</h3>
      <p class="text-sm text-amber-600">Показывается один раз — скопируй и передай поставщику.</p>

      {#if created.handover === 'password'}
        <div class="space-y-2">
          <div>
            <span class="block text-xs text-slate-500">Логин</span>
            <div class="flex items-center gap-2">
              <code class="flex-1 rounded bg-slate-100 px-2 py-1 text-sm">{created.username}</code>
              <button onclick={() => copy(created!.username, 'u')} class="text-xs text-brand-600 hover:underline">{copied === 'u' ? 'ок' : 'копировать'}</button>
            </div>
          </div>
          <div>
            <span class="block text-xs text-slate-500">Пароль</span>
            <div class="flex items-center gap-2">
              <code class="flex-1 rounded bg-slate-100 px-2 py-1 text-sm">{created.password}</code>
              <button onclick={() => copy(created!.password ?? '', 'p')} class="text-xs text-brand-600 hover:underline">{copied === 'p' ? 'ок' : 'копировать'}</button>
            </div>
          </div>
          <div>
            <span class="block text-xs text-slate-500">Страница входа</span>
            <code class="block rounded bg-slate-100 px-2 py-1 text-sm">{created.login_url}</code>
          </div>
        </div>
      {:else}
        <div>
          <span class="block text-xs text-slate-500">Magic-ссылка (вход без пароля)</span>
          <div class="flex items-center gap-2">
            <code class="flex-1 break-all rounded bg-slate-100 px-2 py-1 text-sm">{created.magic_url}</code>
            <button onclick={() => copy(created!.magic_url ?? '', 'm')} class="shrink-0 text-xs text-brand-600 hover:underline">{copied === 'm' ? 'ок' : 'копировать'}</button>
          </div>
        </div>
      {/if}

      {#if created.granted_batches > 0}
        <div class="rounded-md bg-brand-50 px-3 py-2 text-sm text-brand-700">
          Открыт доступ к <b>{created.granted_batches}</b> батч(ам) — поставщик увидит их сразу после входа.
        </div>
      {/if}

      <div class="text-xs text-slate-400">Действует до: {fmt(created.expires_at)}</div>
      <div class="flex justify-end">
        <button onclick={() => (created = null)} class="inline-flex items-center gap-1 rounded-md bg-slate-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-900">
          <Check size={14} /> Готово
        </button>
      </div>
    </div>
  </div>
{/if}
