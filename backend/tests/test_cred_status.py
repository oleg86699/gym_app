"""Тесты единого источника истины статуса cred — generated column cred_status
(миграция 0025) + агрегаты что от него зависят.

Эта логика вызывала большинство рассинхрон-багов между страницами. Тесты
пиннят truth-table прямо против БД (реальное CASE-выражение), чтобы любой
рефактор SQL/предикатов ломал тест, а не прод.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


# (is_valid, last_validation_kind, validated, can_admin_login) -> expected cred_status
# validated=True означает last_validated_at IS NOT NULL.
TRUTH_TABLE = [
    # invalid: is_valid=False — любой kind
    (False, "ok", True, None, "invalid"),
    (False, None, False, None, "invalid"),
    (False, "auth_invalid", True, None, "invalid"),
    # invalid: явный invalid kind даже при is_valid=True
    (True, "auth_invalid", True, None, "invalid"),
    (True, "permission_denied", True, None, "invalid"),
    (True, "manual_invalid", True, None, "invalid"),
    # pending: не валидировали (и не invalid)
    (True, None, False, None, "pending"),
    (True, "ok", False, None, "pending"),  # kind есть, но validated_at NULL
    # valid: Tier 1 ok / manual_valid
    (True, "ok", True, None, "valid"),
    (True, "manual_valid", True, None, "valid"),
    # valid: legacy (kind NULL, но провалидирован)
    (True, None, True, None, "valid"),
    # valid: Tier 2 admin login подтвердил (xmlrpc не ok)
    (True, "xmlrpc_disabled", True, True, "valid"),
    (True, "broken_endpoint", True, True, "valid"),
    # transient: провалидирован, ни один канал не подтвердил
    (True, "xmlrpc_disabled", True, None, "transient"),
    (True, "broken_endpoint", True, False, "transient"),
    (True, "network", True, None, "transient"),
    (True, "server_error", True, None, "transient"),
]


@pytest.mark.parametrize("is_valid,kind,validated,admin,expected", TRUTH_TABLE)
async def test_cred_status_expression(db_session, is_valid, kind, validated, admin, expected):
    """Проверяем CASE-выражение cred_status прямо в БД через VALUES-строку.

    Воспроизводим то же выражение что в generated column, скармливаем
    конкретную комбинацию и сверяем результат. Не пишем в таблицу — гоняем
    выражение на литералах (быстро, без побочек)."""
    sql = text("""
        SELECT CASE
          WHEN :is_valid IS FALSE
               OR :kind IN ('auth_invalid','permission_denied','manual_invalid')
            THEN 'invalid'
          WHEN :validated IS FALSE
            THEN 'pending'
          WHEN :kind IN ('ok','manual_valid')
               OR :kind IS NULL
               OR :admin IS TRUE
            THEN 'valid'
          ELSE 'transient'
        END
    """)
    result = (await db_session.execute(sql, {
        "is_valid": is_valid,
        "kind": kind,
        "validated": validated,
        "admin": admin,
    })).scalar_one()
    assert result == expected, (
        f"({is_valid}, {kind!r}, validated={validated}, admin={admin}) "
        f"→ got {result!r}, expected {expected!r}"
    )


async def test_generated_column_matches_expression(db_session):
    """Реальная generated column в wp_credentials должна совпадать с тем же
    CASE, посчитанным «на лету» из её колонок. Защита от дрейфа определения
    столбца относительно того что ждёт код."""
    mismatches = (await db_session.execute(text("""
        SELECT count(*) FROM wp_credentials
        WHERE deleted_at IS NULL
          AND cred_status <> CASE
            WHEN is_valid IS FALSE
                 OR last_validation_kind IN ('auth_invalid','permission_denied','manual_invalid')
              THEN 'invalid'
            WHEN last_validated_at IS NULL
              THEN 'pending'
            WHEN last_validation_kind IN ('ok','manual_valid')
                 OR last_validation_kind IS NULL
                 OR can_admin_login IS TRUE
              THEN 'valid'
            ELSE 'transient'
          END
    """))).scalar_one()
    assert mismatches == 0


async def test_pool_summary_sums_to_total(db_session):
    """Инвариант: valid+invalid+transient+pending = total cred. Если предикаты
    рассинхронились (как было раньше с NULL-дырами), сумма ≠ total."""
    row = (await db_session.execute(text("""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE cred_status='valid') AS n_valid,
          count(*) FILTER (WHERE cred_status='invalid') AS n_invalid,
          count(*) FILTER (WHERE cred_status='transient') AS n_transient,
          count(*) FILTER (WHERE cred_status='pending') AS n_pending
        FROM wp_credentials WHERE deleted_at IS NULL
    """))).one()
    assert row.n_valid + row.n_invalid + row.n_transient + row.n_pending == row.total
