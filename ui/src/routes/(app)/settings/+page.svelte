<script lang="ts">
  import { AlertTriangle } from 'lucide-svelte'
  import { onMount } from 'svelte'

  import { appSettings as appSettingsApi } from '$lib/api/admin'
  import { ApiError } from '$lib/api/client'
  import type { AppSettings } from '$lib/api/types'
  import { showToast } from '$lib/stores/toast'
  import { currentUser } from '$lib/stores/user'

  let cfg = $state<AppSettings | null>(null)
  let loading = $state(true)
  let saving = $state(false)

  let formConcurrency = $state(25)
  let formTimeout = $state(30)
  let formGlobalConcurrency = $state(80)
  let formCfBrowser = $state(3)
  let formPublishFrom = $state('')   // "" = окно не задано
  let formPublishTo = $state('')

  let canEdit = $derived($currentUser?.is_super_admin === true)

  // Окно публикации back-date'ит: дата позже сегодня недопустима (иначе WP
  // поставит пост в Scheduled и публично спрячет). Ограничиваем To/From.
  const today = new Date().toISOString().slice(0, 10)

  let dirty = $derived(
    cfg !== null &&
      (formConcurrency !== cfg.default_concurrency ||
        formTimeout !== cfg.default_timeout_seconds ||
        formGlobalConcurrency !== cfg.global_posting_concurrency ||
        formCfBrowser !== cfg.cf_browser_concurrency ||
        formPublishFrom !== (cfg.default_publish_from ?? '') ||
        formPublishTo !== (cfg.default_publish_to ?? '')),
  )

  // Окно: либо обе даты, либо обе пустые. From <= To.
  let windowInvalid = $derived.by(() => {
    const a = formPublishFrom
    const b = formPublishTo
    if (!a && !b) return false
    if (!a || !b) return true
    return a > b
  })

  // Защита от устаревшего/ручного значения: дата публикации в будущем.
  let windowHasFuture = $derived(
    (!!formPublishFrom && formPublishFrom > today) ||
      (!!formPublishTo && formPublishTo > today),
  )

  async function load() {
    try {
      cfg = await appSettingsApi.get()
      formConcurrency = cfg.default_concurrency
      formTimeout = cfg.default_timeout_seconds
      formGlobalConcurrency = cfg.global_posting_concurrency
      formCfBrowser = cfg.cf_browser_concurrency
      formPublishFrom = cfg.default_publish_from ?? ''
      formPublishTo = cfg.default_publish_to ?? ''
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      loading = false
    }
  }

  onMount(load)

  async function save(e: SubmitEvent) {
    e.preventDefault()
    if (!cfg || windowInvalid || windowHasFuture) return
    saving = true
    try {
      cfg = await appSettingsApi.update({
        default_concurrency: formConcurrency,
        default_timeout_seconds: formTimeout,
        global_posting_concurrency: formGlobalConcurrency,
        cf_browser_concurrency: formCfBrowser,
        default_publish_from: formPublishFrom || null,
        default_publish_to: formPublishTo || null,
      })
      showToast('success', 'Settings updated')
    } catch (e) {
      showToast('error', e instanceof ApiError ? e.message : String(e))
    } finally {
      saving = false
    }
  }

  function reset() {
    if (!cfg) return
    formConcurrency = cfg.default_concurrency
    formTimeout = cfg.default_timeout_seconds
    formGlobalConcurrency = cfg.global_posting_concurrency
    formCfBrowser = cfg.cf_browser_concurrency
    formPublishFrom = cfg.default_publish_from ?? ''
    formPublishTo = cfg.default_publish_to ?? ''
  }

  function clearWindow() {
    formPublishFrom = ''
    formPublishTo = ''
  }
</script>

<div class="space-y-6">
  <div>
    <h1 class="text-2xl font-semibold text-slate-900">Settings</h1>
    <p class="mt-1 text-sm text-slate-500">
      Глобальные параметры постинга. Применяются ко всем новым прогонам.
    </p>
  </div>

  {#if loading}
    <div class="rounded-lg border border-slate-200 bg-white p-8 text-center text-sm text-slate-500">Loading…</div>
  {:else if !cfg}
    <div class="rounded-lg border border-red-200 bg-white p-8 text-center text-sm text-red-600">
      Failed to load settings.
    </div>
  {:else}
    <section class="rounded-lg border border-slate-200 bg-white p-6">
      <h2 class="text-lg font-medium text-slate-900">Posting defaults</h2>
      <p class="mt-1 text-xs text-slate-500">
        Эти числа жёстко применяются ко всем новым run-ам — менеджеры не могут их переопределить.
      </p>

      <form onsubmit={save} class="mt-4 max-w-xl space-y-5">
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label for="cfg_cc" class="block text-sm font-medium text-slate-700">
              Concurrency <span class="text-slate-400">({cfg.limits.min_concurrency}–{cfg.limits.max_concurrency})</span>
            </label>
            <input id="cfg_cc" type="number" bind:value={formConcurrency}
                   min={cfg.limits.min_concurrency} max={cfg.limits.max_concurrency}
                   disabled={!canEdit}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            <p class="mt-1 text-[11px] text-slate-400">
              Сколько постов воркер делает одновременно внутри одного прогона.
              Больше = быстрее, но больше нагрузки и шансов попасть в rate-limit WP.
            </p>
          </div>
          <div>
            <label for="cfg_to" class="block text-sm font-medium text-slate-700">
              Timeout (sec) <span class="text-slate-400">({cfg.limits.min_timeout_seconds}–{cfg.limits.max_timeout_seconds})</span>
            </label>
            <input id="cfg_to" type="number" bind:value={formTimeout}
                   min={cfg.limits.min_timeout_seconds} max={cfg.limits.max_timeout_seconds}
                   disabled={!canEdit}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            <p class="mt-1 text-[11px] text-slate-400">
              Таймаут одного HTTP-запроса к XML-RPC. Если WP не ответил за N сек —
              попытка провальная, воркер пробует следующую credential / сайт.
            </p>
          </div>
        </div>

        <div class="rounded-md border border-slate-200 bg-slate-50/50 p-4">
          <label for="cfg_gc" class="block text-sm font-medium text-slate-700">
            Global posting capacity
            <span class="text-slate-400">({cfg.limits.min_global_posting_concurrency}–{cfg.limits.max_global_posting_concurrency})</span>
          </label>
          <input id="cfg_gc" type="number" bind:value={formGlobalConcurrency}
                 min={cfg.limits.min_global_posting_concurrency} max={cfg.limits.max_global_posting_concurrency}
                 disabled={!canEdit}
                 class="mt-1 w-40 rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
          <p class="mt-1 text-[11px] text-slate-400">
            Жёсткий потолок <b>одновременных постов через ВСЕ прогоны и оба
            worker-процесса</b> (защищает пул прокси / БД / трафик). Делится между
            активными прогонами → «всё двигается понемногу».
            При global={formGlobalConcurrency} и per-run Concurrency={formConcurrency}
            одновременно на полной скорости идут ≈ <b>{Math.max(1, Math.floor(formGlobalConcurrency / Math.max(1, formConcurrency)))}</b>
            прогона, остальные делят слоты вперемешку. Хочешь больше параллельных
            прогонов «понемногу» — опусти per-run Concurrency.
          </p>
        </div>

        <div class="rounded-md border border-amber-200 bg-amber-50/40 p-4">
          <label for="cfg_cfb" class="block text-sm font-medium text-slate-700">
            CF browser concurrency
            <span class="text-slate-400">({cfg.limits.min_cf_browser_concurrency}–{cfg.limits.max_cf_browser_concurrency})</span>
          </label>
          <input id="cfg_cfb" type="number" bind:value={formCfBrowser}
                 min={cfg.limits.min_cf_browser_concurrency} max={cfg.limits.max_cf_browser_concurrency}
                 disabled={!canEdit}
                 class="mt-1 w-40 rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
          <p class="mt-1 text-[11px] text-slate-400">
            Сколько <b>браузер-контекстов (Patchright)</b> крутим одновременно при обходе
            Cloudflare (Tier 3). Браузер ~150–400 МБ — ставь по RAM сервера: дев-мак 5–10,
            маленький прод-сервер 3–5. Касается только медленной CF-полосы (~8% сайтов).
          </p>
        </div>

        <div class="rounded-md border border-slate-200 p-4">
          <div class="flex items-center justify-between">
            <span class="block text-sm font-medium text-slate-700">Publication date window</span>
            {#if canEdit && (formPublishFrom || formPublishTo)}
              <button type="button" onclick={clearWindow}
                      class="text-xs text-slate-500 hover:text-slate-700 hover:underline">
                Clear
              </button>
            {/if}
          </div>
          <p class="mt-1 text-[11px] text-slate-400">
            Каждому посту воркер ставит случайную дату внутри окна
            <code class="rounded bg-slate-100 px-1">[From, To]</code> — пост
            публикуется этой (прошедшей) датой, поэтому сразу виден и «падает вниз»
            в ленте. Дата позже сегодня недоступна (иначе WordPress спрячет пост
            в Scheduled). Если окно не задано — все посты публикуются текущим моментом.
          </p>
          <div class="mt-3 grid grid-cols-2 gap-4">
            <div>
              <label for="cfg_from" class="block text-xs font-medium uppercase tracking-wider text-slate-500">
                From
              </label>
              <input id="cfg_from" type="date" bind:value={formPublishFrom}
                     disabled={!canEdit} max={formPublishTo || today}
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            </div>
            <div>
              <label for="cfg_to" class="block text-xs font-medium uppercase tracking-wider text-slate-500">
                To
              </label>
              <input id="cfg_to" type="date" bind:value={formPublishTo}
                     disabled={!canEdit} min={formPublishFrom || undefined} max={today}
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            </div>
          </div>
          {#if windowInvalid}
            <p class="mt-2 text-xs text-red-600">
              Заполни обе даты, и From должна быть не позже To.
            </p>
          {:else if windowHasFuture}
            <p class="mt-2 text-xs text-amber-600">
              <AlertTriangle size={14} class="inline-block align-text-bottom" /> Дата позже сегодня — посты уйдут в Scheduled. Выбери дату не позже сегодняшней.
            </p>
          {/if}
        </div>

        {#if canEdit}
          <div class="flex justify-end gap-2 pt-2">
            <button type="button" onclick={reset} disabled={!dirty}
                    class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              Reset
            </button>
            <button type="submit" disabled={saving || windowInvalid || windowHasFuture || !dirty}
                    class="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700 disabled:bg-slate-300">
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        {:else}
          <p class="pt-2 text-xs text-slate-500">
            Только super_admin может менять эти параметры.
          </p>
        {/if}
      </form>
    </section>
  {/if}
</div>
