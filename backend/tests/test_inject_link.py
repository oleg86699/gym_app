"""reuse-инжект: чистка старой ссылки + вставка новой «хорошей логикой»."""

from __future__ import annotations

from domain.text_links import extract_links, inject_link


def _links(html: str):
    return extract_links(html)


def test_strips_old_link_and_injects_contextual():
    # старая ссылка на old.com + анкор «Nawal» встречается в тексте
    html = ('<p>Раніше тут була <a href="https://old.com/">стара</a> ссылка.</p>'
            '<p>Сайт Nawal дуже корисний для всіх.</p>')
    out = inject_link(html, "https://nawal.mx/", "Nawal")
    links = _links(out)
    # старой ссылки нет, есть ровно одна — наша
    assert len(links) == 1
    assert links[0][0] == "https://nawal.mx/"
    assert "old.com" not in out
    # обёрнуто контекстное вхождение «Nawal»
    assert '<a href="https://nawal.mx/">Nawal</a>' in out
    # текст старой ссылки сохранён (unwrap)
    assert "стара" in out


def test_injects_by_significant_word_when_exact_anchor_absent():
    html = "<p>Тут есть слово футбол в предложении про спорт.</p>"
    out = inject_link(html, "https://bet.example/", "ставки на футбол")
    links = _links(out)
    assert len(links) == 1 and links[0][0] == "https://bet.example/"
    # обёрнуто значимое слово «футбол»
    assert "футбол</a>" in out


def test_fallback_inserts_into_paragraph_when_no_match():
    html = ("<p>Совершенно нерелевантный длинный абзац без нужных слов внутри "
            "текста для проверки фолбэка вставки.</p>")
    out = inject_link(html, "https://x.example/", "BrandXYZ")
    links = _links(out)
    assert len(links) == 1 and links[0][0] == "https://x.example/"
    assert "BrandXYZ" in out
    # ссылка попала внутрь абзаца, а не висит отдельно
    assert "<p>" in out and "</a></p>" in out.replace(" ", "")


def test_escapes_anchor_and_link():
    html = "<p>plain paragraph text here for the fallback insertion path ok</p>"
    out = inject_link(html, "https://x.example/?a=1&b=2", "A&B <co>")
    # амперсанд в href экранирован, угловые в анкоре экранированы
    assert "a=1&amp;b=2" in out
    assert "&lt;co&gt;" in out
