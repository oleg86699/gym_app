"""_compute_post_date: окно публикации back-date'ит, но НИКОГДА не в будущее.

Регрессия на баг, из-за которого `effective_start = max(window_start, now)`
зажимал старт на «сегодня» → даты уходили в будущее → WP ставил посты в
Scheduled и публично прятал. Теперь клампим верхнюю границу к now.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from workers.celery.posting import _compute_post_date

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)


def test_no_window_returns_now():
    assert _compute_post_date(None, None, NOW) == NOW
    assert _compute_post_date(date(2026, 6, 1), None, NOW) == NOW
    assert _compute_post_date(None, date(2026, 6, 1), NOW) == NOW


def test_past_window_backdates_within_window():
    f, t = date(2026, 6, 1), date(2026, 6, 10)
    lo = datetime.combine(f, datetime.min.time(), UTC)
    hi = datetime.combine(t, datetime.max.time(), UTC)
    for _ in range(200):
        pd = _compute_post_date(f, t, NOW)
        assert lo <= pd <= hi      # внутри окна
        assert pd <= NOW           # и в прошлом — сразу опубликован


def test_window_ending_in_future_never_schedules_future():
    # текущий «битый» дефолт: окно кончается ПОЗЖЕ сегодня
    f, t = date(2026, 5, 19), date(2026, 7, 3)
    lo = datetime.combine(f, datetime.min.time(), UTC)
    for _ in range(200):
        pd = _compute_post_date(f, t, NOW)
        assert pd <= NOW           # ← ключ: никогда не в будущее (не Scheduled)
        assert pd >= lo


def test_window_entirely_in_future_falls_back_to_now():
    assert _compute_post_date(date(2026, 7, 1), date(2026, 7, 3), NOW) == NOW
