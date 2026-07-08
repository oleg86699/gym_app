<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { Editor } from '@tiptap/core'
  import StarterKit from '@tiptap/starter-kit'
  import Link from '@tiptap/extension-link'
  import Image from '@tiptap/extension-image'
  import Placeholder from '@tiptap/extension-placeholder'
  import Underline from '@tiptap/extension-underline'
  import Table from '@tiptap/extension-table'
  import TableRow from '@tiptap/extension-table-row'
  import TableHeader from '@tiptap/extension-table-header'
  import TableCell from '@tiptap/extension-table-cell'

  interface Props {
    content: string
    readonly?: boolean
    placeholder?: string
    minHeightClass?: string
    onUpdate?: (html: string) => void
  }

  let {
    content = $bindable(''),
    readonly = false,
    placeholder = 'Начните печатать…',
    minHeightClass = 'min-h-[60vh]',
    onUpdate,
  }: Props = $props()

  let editorEl: HTMLDivElement
  let editor: Editor | null = $state(null)

  // Реактивные стейты тулбара
  let active = $state({
    bold: false,
    italic: false,
    underline: false,
    strike: false,
    code: false,
    h1: false,
    h2: false,
    h3: false,
    bulletList: false,
    orderedList: false,
    blockquote: false,
    codeBlock: false,
    link: false,
  })

  function updateActive() {
    if (!editor) return
    active = {
      bold: editor.isActive('bold'),
      italic: editor.isActive('italic'),
      underline: editor.isActive('underline'),
      strike: editor.isActive('strike'),
      code: editor.isActive('code'),
      h1: editor.isActive('heading', { level: 1 }),
      h2: editor.isActive('heading', { level: 2 }),
      h3: editor.isActive('heading', { level: 3 }),
      bulletList: editor.isActive('bulletList'),
      orderedList: editor.isActive('orderedList'),
      blockquote: editor.isActive('blockquote'),
      codeBlock: editor.isActive('codeBlock'),
      link: editor.isActive('link'),
    }
  }

  onMount(() => {
    editor = new Editor({
      element: editorEl,
      extensions: [
        StarterKit.configure({
          heading: { levels: [1, 2, 3] },
        }),
        Underline,
        Link.configure({
          openOnClick: false,
          autolink: true,
          // Backlink-инструмент: НЕ навешиваем rel/target на ссылки — money-ссылка
          // должна оставаться dofollow, как в источнике. nofollow убивал ценность.
          HTMLAttributes: { rel: null, target: null },
        }),
        Image.configure({ inline: false, allowBase64: false }),
        // Таблицы: без них StarterKit схлопывал <table> в плоский <p> (терялась
        // разметка при правке/сохранении). resizable выключен — это постинг-тул,
        // а не WYSIWYG-конструктор; бордеры по умолчанию, чтобы таблица не была
        // «невидимой» после ре-сериализации.
        Table.configure({
          resizable: false,
          HTMLAttributes: {
            border: '1',
            cellpadding: '6',
            cellspacing: '0',
            style: 'border-collapse:collapse; width:100%;',
          },
        }),
        TableRow,
        TableHeader,
        TableCell,
        Placeholder.configure({ placeholder }),
      ],
      content: content || '<p></p>',
      editable: !readonly,
      editorProps: {
        attributes: {
          class:
            'prose prose-slate prose-sm max-w-none focus:outline-none px-4 py-3 ' +
            minHeightClass,
        },
      },
      onUpdate({ editor }) {
        const html = editor.getHTML()
        content = html
        updateActive()
        onUpdate?.(html)
      },
      onSelectionUpdate: updateActive,
      onTransaction: updateActive,
    })
  })

  onDestroy(() => {
    editor?.destroy()
    editor = null
  })

  // Внешний контент изменился (load / revert) — синхронизируем в редактор
  $effect(() => {
    if (!editor) return
    const current = editor.getHTML()
    if (content !== current) {
      editor.commands.setContent(content || '<p></p>', false)
    }
  })

  $effect(() => {
    editor?.setEditable(!readonly)
  })

  // ─── Команды тулбара ────────────────────────────────────────────────

  function toggle(cmd: () => void) {
    cmd()
    editor?.commands.focus()
  }

  function setLink() {
    if (!editor) return
    const prev = editor.getAttributes('link').href as string | undefined
    const url = window.prompt('URL ссылки (пусто = удалить)', prev ?? 'https://')
    if (url === null) return
    if (url === '') {
      editor.chain().focus().extendMarkRange('link').unsetLink().run()
      return
    }
    editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
  }

  function addImage() {
    if (!editor) return
    const url = window.prompt('URL картинки')
    if (!url) return
    editor.chain().focus().setImage({ src: url }).run()
  }
</script>

{#snippet btn(label: string, onClick: () => void, isActive = false, title = '')}
  <button type="button" onclick={onClick} {title}
          disabled={readonly}
          class="rounded px-2 py-1 text-xs font-medium transition"
          class:bg-brand-600={isActive}
          class:text-white={isActive}
          class:text-slate-600={!isActive}
          class:hover:bg-slate-200={!isActive && !readonly}>
    {label}
  </button>
{/snippet}

<div class="overflow-hidden rounded-md border border-slate-300 bg-white">
  <!-- Toolbar -->
  <div class="flex flex-wrap items-center gap-0.5 border-b border-slate-200 bg-slate-50 px-2 py-1.5">
    {@render btn('B', () => toggle(() => editor?.chain().focus().toggleBold().run()), active.bold, 'Bold (Cmd+B)')}
    {@render btn('I', () => toggle(() => editor?.chain().focus().toggleItalic().run()), active.italic, 'Italic (Cmd+I)')}
    {@render btn('U', () => toggle(() => editor?.chain().focus().toggleUnderline().run()), active.underline, 'Underline')}
    {@render btn('S', () => toggle(() => editor?.chain().focus().toggleStrike().run()), active.strike, 'Strikethrough')}
    <span class="mx-1 h-4 w-px bg-slate-300"></span>
    {@render btn('H1', () => toggle(() => editor?.chain().focus().toggleHeading({ level: 1 }).run()), active.h1)}
    {@render btn('H2', () => toggle(() => editor?.chain().focus().toggleHeading({ level: 2 }).run()), active.h2)}
    {@render btn('H3', () => toggle(() => editor?.chain().focus().toggleHeading({ level: 3 }).run()), active.h3)}
    {@render btn('P', () => toggle(() => editor?.chain().focus().setParagraph().run()), false, 'Paragraph')}
    <span class="mx-1 h-4 w-px bg-slate-300"></span>
    {@render btn('• List', () => toggle(() => editor?.chain().focus().toggleBulletList().run()), active.bulletList)}
    {@render btn('1. List', () => toggle(() => editor?.chain().focus().toggleOrderedList().run()), active.orderedList)}
    {@render btn('“ Quote', () => toggle(() => editor?.chain().focus().toggleBlockquote().run()), active.blockquote)}
    {@render btn('</> Code', () => toggle(() => editor?.chain().focus().toggleCode().run()), active.code, 'Inline code')}
    {@render btn('Code block', () => toggle(() => editor?.chain().focus().toggleCodeBlock().run()), active.codeBlock)}
    {@render btn('— Rule', () => toggle(() => editor?.chain().focus().setHorizontalRule().run()))}
    <span class="mx-1 h-4 w-px bg-slate-300"></span>
    {@render btn('Link', setLink, active.link)}
    {@render btn('Image', addImage)}
    <span class="mx-1 h-4 w-px bg-slate-300"></span>
    {@render btn('Clear', () => toggle(() => editor?.chain().focus().unsetAllMarks().clearNodes().run()), false, 'Очистить форматирование')}
    <span class="ml-auto flex gap-0.5">
      {@render btn('↶', () => toggle(() => editor?.chain().focus().undo().run()), false, 'Undo (Cmd+Z)')}
      {@render btn('↷', () => toggle(() => editor?.chain().focus().redo().run()), false, 'Redo (Cmd+Shift+Z)')}
    </span>
  </div>

  <!-- Editor -->
  <div bind:this={editorEl}></div>
</div>

<style>
  /* tiptap prose styling — мы сами добавили tailwind-typography классы выше,
     но если он не подключен, тут базовый fallback чтобы редактор выглядел нормально */
  :global(.ProseMirror) {
    outline: none;
    min-height: 200px;
  }
  :global(.ProseMirror p) { margin: 0 0 0.75em; }
  :global(.ProseMirror h1) { font-size: 1.6em; font-weight: 700; margin: 1em 0 0.5em; }
  :global(.ProseMirror h2) { font-size: 1.35em; font-weight: 700; margin: 1em 0 0.5em; }
  :global(.ProseMirror h3) { font-size: 1.15em; font-weight: 600; margin: 1em 0 0.5em; }
  :global(.ProseMirror ul) { list-style: disc; padding-left: 1.5em; margin: 0 0 0.75em; }
  :global(.ProseMirror ol) { list-style: decimal; padding-left: 1.5em; margin: 0 0 0.75em; }
  :global(.ProseMirror blockquote) {
    border-left: 3px solid #cbd5e1; padding-left: 12px;
    color: #475569; margin: 0 0 0.75em;
  }
  :global(.ProseMirror code) {
    background: #f1f5f9; padding: 1px 4px; border-radius: 3px;
    font-size: 0.92em; font-family: ui-monospace, monospace;
  }
  :global(.ProseMirror pre) {
    background: #0f172a; color: #f1f5f9; padding: 12px;
    border-radius: 6px; overflow-x: auto; margin: 0 0 0.75em;
  }
  :global(.ProseMirror pre code) { background: transparent; padding: 0; color: inherit; }
  :global(.ProseMirror a) { color: #4f46e5; text-decoration: underline; }
  :global(.ProseMirror img) { max-width: 100%; height: auto; border-radius: 4px; }
  :global(.ProseMirror hr) { border: 0; border-top: 1px solid #cbd5e1; margin: 1em 0; }
  :global(.ProseMirror table) {
    border-collapse: collapse; width: 100%; margin: 0 0 0.75em; font-size: 0.95em;
  }
  :global(.ProseMirror th), :global(.ProseMirror td) {
    border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; vertical-align: top;
  }
  :global(.ProseMirror th) { background: #f1f5f9; font-weight: 600; }
  /* placeholder */
  :global(.ProseMirror p.is-editor-empty:first-child::before) {
    color: #94a3b8;
    content: attr(data-placeholder);
    float: left;
    height: 0;
    pointer-events: none;
  }
</style>
