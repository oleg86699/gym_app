<script lang="ts">
  import { HelpCircle } from 'lucide-svelte'
  import { onDestroy } from 'svelte'

  interface Props {
    align?: 'left' | 'right'
  }
  let { align = 'right' }: Props = $props()

  let open = $state(false)
  let containerEl: HTMLDivElement

  // Те же цвета, что и в roleBadge() на страницах wp-sites / batches.
  const ROLES: { role: string; cls: string; can: string; canPost: boolean; pub: boolean }[] = [
    { role: 'administrator', cls: 'bg-purple-100 text-purple-700',
      can: 'Всё: плагины, темы, настройки, создавать пользователей', canPost: true, pub: true },
    { role: 'editor', cls: 'bg-blue-100 text-blue-700',
      can: 'Публикует/редактирует ЛЮБЫЕ посты и страницы, модерирует комментарии', canPost: true, pub: true },
    { role: 'author', cls: 'bg-emerald-100 text-emerald-700',
      can: 'Пишет, грузит медиа и публикует СВОИ посты', canPost: true, pub: true },
    { role: 'contributor', cls: 'bg-amber-100 text-amber-700',
      can: 'Только черновики своих постов — НЕ публикует, не грузит медиа', canPost: false, pub: false },
    { role: 'subscriber', cls: 'bg-slate-100 text-slate-500',
      can: 'Только читает и редактирует свой профиль', canPost: false, pub: false },
  ]

  function onDocClick(e: MouseEvent) {
    if (open && containerEl && !containerEl.contains(e.target as Node)) open = false
  }
  function onEsc(e: KeyboardEvent) {
    if (e.key === 'Escape') open = false
  }
  $effect(() => {
    if (open) {
      document.addEventListener('click', onDocClick)
      document.addEventListener('keydown', onEsc)
    } else {
      document.removeEventListener('click', onDocClick)
      document.removeEventListener('keydown', onEsc)
    }
  })
  onDestroy(() => {
    document.removeEventListener('click', onDocClick)
    document.removeEventListener('keydown', onEsc)
  })
</script>

<div bind:this={containerEl} class="relative inline-block align-middle">
  <button type="button"
          title="Справка по WP-ролям"
          aria-label="Справка по WP-ролям"
          onclick={(e) => { e.stopPropagation(); open = !open }}
          class="inline-flex items-center text-slate-400 hover:text-slate-600">
    <HelpCircle size={13} />
  </button>

  {#if open}
    <div
      class="absolute z-30 mt-1 w-[440px] max-w-[90vw] rounded-lg border border-slate-200 bg-white p-3 text-left shadow-xl"
      class:right-0={align === 'right'}
      class:left-0={align === 'left'}
      role="dialog"
    >
      <div class="mb-2 text-xs font-semibold text-slate-700">
        Роли WordPress <span class="font-normal text-slate-400">(от старшей к младшей)</span>
      </div>
      <table class="w-full border-collapse text-[11px]">
        <thead>
          <tr class="text-slate-400">
            <th class="pb-1 pr-2 text-left font-medium">Роль</th>
            <th class="pb-1 pr-2 text-left font-medium">Может</th>
            <th class="pb-1 text-center font-medium" title="Может ли реально опубликовать пост (publish_posts)">Постинг</th>
          </tr>
        </thead>
        <tbody>
          {#each ROLES as r}
            <tr class="border-t border-slate-100 align-top">
              <td class="py-1 pr-2">
                <span class="inline-block rounded-full px-1.5 py-0.5 text-[10px] font-medium {r.cls}">{r.role}</span>
              </td>
              <td class="py-1 pr-2 leading-snug text-slate-600">{r.can}</td>
              <td class="py-1 text-center">
                {#if r.pub}
                  <span class="font-semibold text-emerald-600" title="Может публиковать">✓</span>
                {:else}
                  <span class="font-semibold text-red-500" title="Не публикует — пост повиснет черновиком">✗</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <div class="mt-2 border-t border-slate-100 pt-2 text-[10px] leading-snug text-slate-400">
        Для постинга годятся роли с правом публикации — <span class="text-emerald-600">administrator / editor / author</span>.
        У <span class="text-amber-600">contributor</span> и <span class="text-slate-500">subscriber</span> пост не опубликуется.
        <br />
        <span class="text-slate-500">Кастомные роли</span> от плагинов (напр. <span class="font-medium">seller</span>/<span class="font-medium">vendor</span>
        из Dokan/WCFM/WC&nbsp;Vendors, <span class="font-medium">shop_manager</span> из WooCommerce, и любые другие)
        показываются как есть — права у них произвольные, <span class="text-amber-600">публикация под вопросом</span>:
        проверяется по факту реальной попытки поста.
      </div>
    </div>
  {/if}
</div>
