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
  let formFloor = $state(5)
  let formSiteDisable = $state(25)
  let formSiteDisableCf = $state(8)
  let formMaxConcurrentBatches = $state(3)
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
        formFloor !== cfg.posting_concurrency_floor ||
        formSiteDisable !== cfg.site_disable_threshold ||
        formSiteDisableCf !== cfg.site_disable_threshold_cf ||
        formMaxConcurrentBatches !== cfg.max_concurrent_batch_validations ||
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
      formFloor = cfg.posting_concurrency_floor
      formSiteDisable = cfg.site_disable_threshold
      formSiteDisableCf = cfg.site_disable_threshold_cf
      formMaxConcurrentBatches = cfg.max_concurrent_batch_validations
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
        posting_concurrency_floor: formFloor,
        site_disable_threshold: formSiteDisable,
        site_disable_threshold_cf: formSiteDisableCf,
        max_concurrent_batch_validations: formMaxConcurrentBatches,
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
    formFloor = cfg.posting_concurrency_floor
    formSiteDisable = cfg.site_disable_threshold
    formSiteDisableCf = cfg.site_disable_threshold_cf
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
              Max concurrency / run <span class="text-slate-400">({cfg.limits.min_concurrency}–{cfg.limits.max_concurrency})</span>
            </label>
            <input id="cfg_cc" type="number" bind:value={formConcurrency}
                   min={cfg.limits.min_concurrency} max={cfg.limits.max_concurrency}
                   disabled={!canEdit}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            <p class="mt-1 text-[11px] text-slate-400">
              <b>Потолок</b>, до которого один прогон может разогнаться, когда сервер
              свободен. Реальная конкурентность = <code class="rounded bg-slate-100 px-1">global ÷ активные прогоны</code>,
              но не выше этого числа и не ниже floor. Чтобы одинокий прогон забивал
              весь сервер — ставь ≈ Global capacity.
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
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label for="cfg_gc" class="block text-sm font-medium text-slate-700">
                Global posting capacity
                <span class="text-slate-400">({cfg.limits.min_global_posting_concurrency}–{cfg.limits.max_global_posting_concurrency})</span>
              </label>
              <input id="cfg_gc" type="number" bind:value={formGlobalConcurrency}
                     min={cfg.limits.min_global_posting_concurrency} max={cfg.limits.max_global_posting_concurrency}
                     disabled={!canEdit}
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            </div>
            <div>
              <label for="cfg_floor" class="block text-sm font-medium text-slate-700">
                Min concurrency / run <span class="text-slate-400">({cfg.limits.min_concurrency_floor}–{cfg.limits.max_concurrency_floor})</span>
              </label>
              <input id="cfg_floor" type="number" bind:value={formFloor}
                     min={cfg.limits.min_concurrency_floor} max={cfg.limits.max_concurrency_floor}
                     disabled={!canEdit}
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            </div>
          </div>
          <p class="mt-2 text-[11px] text-slate-400">
            Global — жёсткий потолок <b>одновременных постов через ВСЕ прогоны и оба
            worker-процесса</b> (защищает пул прокси / БД / трафик). Делится между
            активными прогонами по принципу <b>fair-share</b>: каждый прогон получает
            ≈ <code class="rounded bg-slate-100 px-1">global ÷ активные</code>, но не ниже
            <b>floor</b> (чтобы никто не голодал) и не выше Max/run. Один прогон в одиночку
            забивает весь сервер; при {formFloor > 0 ? Math.max(1, Math.floor(formGlobalConcurrency / formFloor)) : '∞'}+
            одновременных прогонах включается floor={formFloor}.
            Под текущий 16ГБ-бокс (вместе с другим сервисом) разумно ~40; на отдельном крупном — 80-150.
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
          <span class="block text-sm font-medium text-slate-700">Авто-выключение мёртвых сайтов</span>
          <p class="mt-1 text-[11px] text-slate-400">
            Сколько site-class фейлов <b>подряд</b> (502/timeout/CF/…) до выключения сайта.
            Любой успешный пост сбрасывает счётчик в 0. Срабатывает <b>даже если у сайта есть
            рабочий логин</b> — чтобы «протухшие» домены не жгли слоты бесконечно.
          </p>
          <div class="mt-3 grid grid-cols-2 gap-4">
            <div>
              <label for="cfg_sd" class="block text-xs font-medium uppercase tracking-wider text-slate-500">
                Общий порог <span class="text-slate-400 normal-case">({cfg.limits.min_site_disable_threshold}–{cfg.limits.max_site_disable_threshold})</span>
              </label>
              <input id="cfg_sd" type="number" bind:value={formSiteDisable}
                     min={cfg.limits.min_site_disable_threshold} max={cfg.limits.max_site_disable_threshold}
                     disabled={!canEdit}
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            </div>
            <div>
              <label for="cfg_sdcf" class="block text-xs font-medium uppercase tracking-wider text-slate-500">
                Порог CF <span class="text-slate-400 normal-case">({cfg.limits.min_site_disable_threshold_cf}–{cfg.limits.max_site_disable_threshold_cf})</span>
              </label>
              <input id="cfg_sdcf" type="number" bind:value={formSiteDisableCf}
                     min={cfg.limits.min_site_disable_threshold_cf} max={cfg.limits.max_site_disable_threshold_cf}
                     disabled={!canEdit}
                     class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
            </div>
          </div>
          <p class="mt-2 text-[11px] text-slate-400">
            CF отдельно агрессивнее: сайт под Cloudflare почти не «оживает» сам, а каждый
            headful-фейл ~30 сек. Рекоменд.: общий <b>25</b>, CF <b>8</b>.
          </p>
        </div>

        <div class="rounded-md border border-slate-200 p-4">
          <h3 class="text-sm font-semibold text-slate-800">Валидация батчей</h3>
          <div class="mt-3 max-w-xs">
            <label for="cfg_mcbv" class="block text-xs font-medium uppercase tracking-wider text-slate-500">
              Одновременных батчей <span class="text-slate-400 normal-case">({cfg.limits.min_max_concurrent_batch_validations}–{cfg.limits.max_max_concurrent_batch_validations})</span>
            </label>
            <input id="cfg_mcbv" type="number" bind:value={formMaxConcurrentBatches}
                   min={cfg.limits.min_max_concurrent_batch_validations} max={cfg.limits.max_max_concurrent_batch_validations}
                   disabled={!canEdit}
                   class="mt-1 w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm disabled:bg-slate-100" />
          </div>
          <p class="mt-2 text-[11px] text-slate-400">
            Сколько батчей валидируется <b>одновременно</b>. Остальные ждут в статусе
            <b>«в очереди»</b> и поднимаются по мере освобождения слотов — чтобы загрузка
            кучи файлов разом не плодила сотни потоков/браузеров. Рекоменд.: <b>3</b>.
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
