/**
 * Копирование в буфер обмена с фолбэком для НЕ-secure контекста.
 *
 * `navigator.clipboard` доступен только в secure context (HTTPS либо
 * localhost/127.0.0.1). prod_b отдаётся по HTTP с голого IP
 * (http://185.225.226.205:8090) → secure context отсутствует → вызов
 * бросает "access denied". Поэтому откатываемся на legacy
 * `document.execCommand('copy')` через скрытую textarea — он работает и
 * по HTTP. Локально (localhost) срабатывает первая, современная ветка.
 *
 * @returns true если скопировано, иначе false — вызывающий сам решает,
 *          какой тост/флаг показать.
 */
export async function copyText(text: string): Promise<boolean> {
  // 1. Современный Clipboard API — только в secure context.
  if (
    typeof navigator !== 'undefined' &&
    navigator.clipboard &&
    typeof window !== 'undefined' &&
    window.isSecureContext
  ) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Падаем в legacy-фолбэк ниже (например, отозванное разрешение).
    }
  }

  // 2. Legacy-фолбэк: скрытая textarea + execCommand('copy'). Работает по HTTP.
  if (typeof document === 'undefined') return false
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    // Вне вьюпорта, чтобы не дёргать скролл и не мигать элементом.
    ta.style.position = 'fixed'
    ta.style.top = '-9999px'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}
