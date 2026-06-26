<script lang="ts">
  /**
   * Редактируемый HTML-source с подсветкой синтаксиса.
   *
   * Техника overlay: подсвеченный <pre><code> рисуется ПОД прозрачной <textarea>
   * (текст textarea прозрачный, виден только каретка). Обе «слойки» имеют
   * идентичные шрифт/размер/межстрочный/паддинг/перенос — поэтому подсветка
   * точно совпадает с курсором. Скролл синхронизируем.
   *
   * Весь код-контент эскейпится → {@html} безопасен (XSS нет; вставляем только
   * свои <span>-ы подсветки).
   */
  interface Props {
    value: string
    readonly?: boolean
    minHeightClass?: string
    oninput?: (v: string) => void
  }
  let {
    value = $bindable(''),
    readonly = false,
    minHeightClass = 'min-h-[60vh]',
    oninput,
  }: Props = $props()

  let ta: HTMLTextAreaElement
  let pre: HTMLPreElement

  function esc(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  }

  function hlAttrs(attrs: string): string {
    let out = ''
    const re = /([\w:-]+)(\s*=\s*)("[^"]*"|'[^']*')|(\s+)|([^\s]+)/g
    let m: RegExpExecArray | null
    while ((m = re.exec(attrs))) {
      if (m[1]) {
        out += `<span class="t-attr">${esc(m[1])}</span><span class="t-punct">${esc(m[2])}</span><span class="t-str">${esc(m[3])}</span>`
      } else if (m[4]) {
        out += esc(m[4])
      } else {
        out += `<span class="t-attr">${esc(m[5])}</span>`
      }
    }
    return out
  }

  function highlight(code: string): string {
    let out = ''
    const re = /(<!--[\s\S]*?-->)|(<\/?)([a-zA-Z][\w:-]*)((?:[^<>"']|"[^"]*"|'[^']*')*?)(\/?>)/g
    let last = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(code))) {
      out += `<span class="t-text">${esc(code.slice(last, m.index))}</span>`
      if (m[1]) {
        out += `<span class="t-comment">${esc(m[1])}</span>`
      } else {
        out += `<span class="t-punct">${esc(m[2])}</span><span class="t-tag">${esc(m[3])}</span>`
        out += hlAttrs(m[4])
        out += `<span class="t-punct">${esc(m[5])}</span>`
      }
      last = re.lastIndex
    }
    out += `<span class="t-text">${esc(code.slice(last))}</span>`
    return out
  }

  // trailing \n — чтобы подсветка не «обрезала» последнюю строку при пустом конце
  let highlighted = $derived(highlight(value) + '\n')

  function onInput(e: Event) {
    value = (e.currentTarget as HTMLTextAreaElement).value
    oninput?.(value)
  }
  function onScroll() {
    if (pre && ta) {
      pre.scrollTop = ta.scrollTop
      pre.scrollLeft = ta.scrollLeft
    }
  }
</script>

<div class="relative w-full overflow-hidden rounded-md border border-slate-300 bg-white {minHeightClass}">
  <pre bind:this={pre} aria-hidden="true"
       class="src-layer pointer-events-none absolute inset-0 m-0 overflow-auto"><code>{@html highlighted}</code></pre>
  <textarea bind:this={ta} {value} oninput={onInput} onscroll={onScroll}
            {readonly} spellcheck="false"
            class="src-layer absolute inset-0 h-full w-full resize-none overflow-auto bg-transparent text-transparent caret-slate-800 focus:outline-none focus:ring-1 focus:ring-brand-500"
  ></textarea>
</div>

<style>
  .src-layer {
    padding: 0.5rem 0.75rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 13px;
    line-height: 1.45;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    word-break: break-word;
    tab-size: 2;
    border: 0;
  }
  pre.src-layer { color: #1e293b; }
  pre.src-layer :global(.t-tag) { color: #2563eb; }
  pre.src-layer :global(.t-attr) { color: #b45309; }
  pre.src-layer :global(.t-str) { color: #15803d; }
  pre.src-layer :global(.t-punct) { color: #64748b; }
  pre.src-layer :global(.t-text) { color: #1e293b; }
  pre.src-layer :global(.t-comment) { color: #94a3b8; font-style: italic; }
</style>
