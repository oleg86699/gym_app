/**
 * Pretty-print HTML для режима «HTML source» редактора: блок-теги — каждый с
 * новой строки и с отступами, а ИНЛАЙН-контент (внутри <p>, <li> и т.п.)
 * остаётся на одной строке.
 *
 * Почему так: WordPress на выводе прогоняет контент через `wpautop`, который из
 * переносов строк ВНУТРИ абзаца делает <br>. Поэтому переносы добавляем только
 * МЕЖДУ блок-тегами (это wpautop переваривает без артефактов), а текст/инлайн
 * не трогаем — публикуемый пост выглядит идентично.
 *
 * Идемпотентно: уже отформатированный вход даёт тот же результат (пробельные
 * текст-ноды между блоками тримятся и отбрасываются).
 */

const BLOCK = new Set([
  'address', 'article', 'aside', 'blockquote', 'details', 'dialog', 'dd', 'div',
  'dl', 'dt', 'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2',
  'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'li', 'main', 'nav', 'ol',
  'p', 'pre', 'section', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'ul',
])

function openTag(el: Element): string {
  let s = '<' + el.tagName.toLowerCase()
  for (const a of Array.from(el.attributes)) {
    s += ` ${a.name}="${(a.value ?? '').replace(/"/g, '&quot;')}"`
  }
  return s + '>'
}

const isBlock = (el: Element) => BLOCK.has(el.tagName.toLowerCase())
const hasBlockChild = (el: Element) =>
  Array.from(el.children).some((c) => isBlock(c))

export function formatHtml(html: string | null | undefined): string {
  if (!html) return ''
  if (typeof DOMParser === 'undefined') return html
  let doc: Document
  try {
    doc = new DOMParser().parseFromString(html, 'text/html')
  } catch {
    return html
  }
  const HEADING = new Set(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
  const out: string[] = []
  function walk(node: Node, depth: number): void {
    const pad = '  '.repeat(depth)
    node.childNodes.forEach((child) => {
      if (child.nodeType === 3) {
        const t = (child.textContent ?? '').trim()
        if (t) out.push(pad + t)
      } else if (child.nodeType === 1) {
        const el = child as Element
        const tag = el.tagName.toLowerCase()
        // Пустая строка перед заголовком верхнего уровня — визуально делит секции.
        if (depth === 0 && HEADING.has(tag) && out.length > 0 && out[out.length - 1] !== '') {
          out.push('')
        }
        if (isBlock(el) && hasBlockChild(el)) {
          out.push(pad + openTag(el))
          walk(el, depth + 1)
          out.push(pad + `</${tag}>`)
        } else {
          // блок с инлайн-контентом ИЛИ инлайн-элемент → одной строкой
          out.push(pad + (el as HTMLElement).outerHTML)
        }
      }
    })
  }
  walk(doc.body, 0)
  return out.join('\n')
}
