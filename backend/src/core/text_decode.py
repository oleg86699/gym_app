"""Устойчивое декодирование текстовых загрузок (артикли, CSV/anchor-тексты).

Многие файлы (особенно турецкие) сохранены в Windows-1254 / cp1252, а не в
UTF-8. Прежний `raw.decode("utf-8", errors="replace")` превращал ç/ö/ü/ğ/ş/ı в
U+FFFD (�) БЕЗВОЗВРАТНО — и в редакторе, и в опубликованном посте.

Стратегия (без внешних зависимостей — chardet/charset-normalizer не установлены):
строгий UTF-8 (правильный случай, ничего не ломаем) → cp1254 (турецкий Windows)
→ cp1252 (западный) → latin-1 (декодирует любые байты 1:1, не падает — рубеж).
"""

from __future__ import annotations

# Порядок важен: cp1254 раньше cp1252, т.к. контент преимущественно турецкий
# (различаются лишь 6 позиций: ğ ş ı İ Ğ Ş).
_FALLBACK_ENCODINGS = ("utf-8-sig", "cp1254", "cp1252")


def decode_text(raw: bytes) -> str:
    """Декодировать байты текста, подбирая кодировку. Никогда не бросает."""
    if not raw:
        return ""
    for enc in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")  # 1:1, не падает
