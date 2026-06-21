"""Spintax-расшивка spin() — детерминированный разворот {a|b|c} (C1)."""

from __future__ import annotations

import random

from domain.text_links import spin


def test_no_braces_unchanged():
    assert spin("plain text") == "plain text"
    assert spin("") == ""
    assert spin(None) == ""


def test_simple_choice():
    rng = random.Random(0)
    out = spin("{red|green|blue}", rng=rng)
    assert out in ("red", "green", "blue")


def test_multiple_groups():
    out = spin("{a|b} middle {x|y}", rng=random.Random(1))
    assert out.split()[0] in ("a", "b")
    assert out.split()[-1] in ("x", "y")
    assert "middle" in out


def test_nested():
    # вложенность: раскрываем изнутри наружу
    results = {spin("{outer|{inner1|inner2}}", rng=random.Random(i)) for i in range(50)}
    assert results <= {"outer", "inner1", "inner2"}
    assert "{" not in "".join(results) and "}" not in "".join(results)


def test_seeded_reproducible():
    a = spin("{1|2|3|4|5} {a|b|c}", rng=random.Random(42))
    b = spin("{1|2|3|4|5} {a|b|c}", rng=random.Random(42))
    assert a == b  # один seed → один результат


def test_produces_variety():
    # на 200 расшивок широкого спина получаем >1 уникальных (уникализация работает)
    variants = {spin("{a|b|c|d|e} {1|2|3|4|5}", rng=random.Random(i)) for i in range(200)}
    assert len(variants) > 5
