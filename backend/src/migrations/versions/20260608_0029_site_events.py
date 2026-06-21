"""site_events — append-only лог ошибок по сайтам (для аналитики).

ЗАЧЕМ: до этого ошибки хранились только «последняя» (перезатиралась) на
wp_sites/wp_credentials. Теперь — полная история событий: и валидация, и
постинг пишут сюда failure-событие. Даёт:
  - таймлайн по каждому сайту (UI вкладка «История ошибок»)
  - аналитику: топ error_kind, динамика, корреляция с прокси
  - отчёт поставщикам доступов «что именно не так с их сайтами»

ОБЪЁМ: фейлы bounded (site-fail counter ограничивает ~10 на cred до
auto-disable), успехи НЕ логируем (они в text_items). На 600k сайтов ≈
1 ГБ старта + ~0.5-1 ГБ/год. Помесячные RANGE-партиции → старые в S3.

Партиционирование по created_at (как text_items): дешёвый архив + maintenance.

Revision ID: 0029_site_events
Revises: 0028_mv_channel_counts
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0029_site_events"
down_revision = "0028_mv_channel_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS site_events_id_seq")
    op.execute("""
        CREATE TABLE site_events (
            id bigint NOT NULL DEFAULT nextval('site_events_id_seq'),
            site_id integer NOT NULL,
            credential_id integer,
            source varchar(16) NOT NULL,        -- 'validation' | 'posting'
            error_kind varchar(32) NOT NULL,    -- xmlrpc_disabled / auth_invalid / cf_challenge ...
            error_message varchar(500),
            posting_run_id integer,
            proxy_id integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT site_events_pkey PRIMARY KEY (created_at, id)
        ) PARTITION BY RANGE (created_at)
    """)
    op.execute("ALTER SEQUENCE site_events_id_seq OWNED BY site_events.id")
    # Индексы (пропагируются на партиции)
    op.execute("CREATE INDEX ix_site_events_site_id ON site_events (site_id, created_at DESC)")
    op.execute("CREATE INDEX ix_site_events_kind ON site_events (error_kind)")
    op.execute("CREATE INDEX ix_site_events_run ON site_events (posting_run_id)")
    # DEFAULT + текущий/следующий месяц (future-партиции создаёт cron)
    op.execute("CREATE TABLE site_events_default PARTITION OF site_events DEFAULT")
    op.execute("""CREATE TABLE site_events_2026_06 PARTITION OF site_events
                  FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')""")
    op.execute("""CREATE TABLE site_events_2026_07 PARTITION OF site_events
                  FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS site_events")
    op.execute("DROP SEQUENCE IF EXISTS site_events_id_seq")
