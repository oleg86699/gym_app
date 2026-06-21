"""Партиционирование text_items по месяцам (RANGE created_at).

ЗАЧЕМ: text_items — самая высокочастотная таблица (на проде ~12M+ строк/год).
Это append-mostly time-series: каждый posting-run пишет тысячи строк, старые
почти не запрашиваются после завершения run-а. Делаем СЕЙЧАС пока таблица
крошечная (≈40 строк) — конверсия тривиальна, риск минимален.

ЧТО ДАЁТ:
  - Дешёвый архив: DETACH старой месячной партиции → pg_dump → S3 → DROP.
    Без партиций удаление миллионов старых строк = долгий DELETE + bloat.
  - Лучше autovacuum / maintenance (по партиции, а не по 12M-таблице).
  - Партиции можно класть на разные tablespace (hot SSD / cold HDD).

КОМПРОМИСС: PG требует partition-key в PK → PK становится (created_at, id).
Это запрещает UNIQUE(id), поэтому входящий FK project_wp_used.text_item_id →
text_items(id) снимаем; text_item_id становится soft-ref (nullable int без FK).
project_wp_used — dedup/tracking таблица, hard-integrity к точному text_item
там не критична (факты project/site/run остаются).

Future-партиции создаёт cron `wp.ensure_text_items_partition` (см. cron_tasks).

Revision ID: 0027_partition_text_items
Revises: 0026_pool_summary_mv
Create Date: 2026-06-06
"""

from __future__ import annotations

from alembic import op

revision = "0027_partition_text_items"
down_revision = "0026_pool_summary_mv"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Снять входящий FK (text_item_id → soft-ref)
    op.execute("ALTER TABLE project_wp_used DROP CONSTRAINT IF EXISTS project_wp_used_text_item_id_fkey")

    # 2. Отвязать sequence от старой таблицы и переименовать её
    op.execute("ALTER SEQUENCE text_items_id_seq OWNED BY NONE")
    op.execute("ALTER TABLE text_items RENAME TO text_items_legacy")
    # Переименовать ВСЕ старые индексы → иначе конфликт с одноимёнными новыми.
    # (legacy таблица всё равно дропается в конце вместе со своими индексами.)
    for idx in (
        "text_items_pkey",
        "ix_text_items_content_hash",
        "ix_text_items_credential_id",
        "ix_text_items_posting_run_id",
        "ix_text_items_project_id",
        "ix_text_items_project_status",
        "ix_text_items_run_status",
        "ix_text_items_site_id",
    ):
        op.execute(f"ALTER INDEX IF EXISTS {idx} RENAME TO {idx}_legacy")

    # 3. Создать партиционированную таблицу. PK = (created_at, id).
    op.execute("""
        CREATE TABLE text_items (
            id integer NOT NULL DEFAULT nextval('text_items_id_seq'),
            posting_run_id integer NOT NULL,
            project_id integer NOT NULL,
            storage_key varchar(500),
            original_filename varchar(500) NOT NULL,
            title varchar(1000),
            content_hash char(64) NOT NULL,
            byte_size integer NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            posted_url text,
            post_id bigint,
            posted_at timestamptz,
            attempts integer NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            credential_id integer,
            site_id integer,
            CONSTRAINT text_items_pkey PRIMARY KEY (created_at, id)
        ) PARTITION BY RANGE (created_at)
    """)
    op.execute("ALTER SEQUENCE text_items_id_seq OWNED BY text_items.id")

    # 4. Индексы на parent (пропагируются на партиции)
    op.execute("CREATE INDEX ix_text_items_id ON text_items (id)")
    op.execute("CREATE INDEX ix_text_items_content_hash ON text_items (content_hash)")
    op.execute("CREATE INDEX ix_text_items_credential_id ON text_items (credential_id)")
    op.execute("CREATE INDEX ix_text_items_posting_run_id ON text_items (posting_run_id)")
    op.execute("CREATE INDEX ix_text_items_project_id ON text_items (project_id)")
    op.execute("CREATE INDEX ix_text_items_project_status ON text_items (project_id, status)")
    op.execute("CREATE INDEX ix_text_items_run_status ON text_items (posting_run_id, status)")
    op.execute("CREATE INDEX ix_text_items_site_id ON text_items (site_id)")

    # 5. Outgoing FK (на партиционированной таблице — OK)
    op.execute("""ALTER TABLE text_items ADD CONSTRAINT text_items_posting_run_id_fkey
                  FOREIGN KEY (posting_run_id) REFERENCES posting_runs(id) ON DELETE CASCADE""")
    op.execute("""ALTER TABLE text_items ADD CONSTRAINT text_items_project_id_fkey
                  FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE""")
    op.execute("""ALTER TABLE text_items ADD CONSTRAINT fk_text_items_credential
                  FOREIGN KEY (credential_id) REFERENCES wp_credentials(id) ON DELETE SET NULL""")
    op.execute("""ALTER TABLE text_items ADD CONSTRAINT fk_text_items_site
                  FOREIGN KEY (site_id) REFERENCES wp_sites(id) ON DELETE SET NULL""")

    # 6. Партиции: DEFAULT (catch-all для старых/будущих) + текущий и следующий месяц.
    #    DEFAULT гарантирует что ни одна вставка не упадёт «no partition».
    op.execute("CREATE TABLE text_items_default PARTITION OF text_items DEFAULT")
    op.execute("""CREATE TABLE text_items_2026_06 PARTITION OF text_items
                  FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')""")
    op.execute("""CREATE TABLE text_items_2026_07 PARTITION OF text_items
                  FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')""")

    # 7. Перелить данные (роутятся по created_at автоматически)
    op.execute("""
        INSERT INTO text_items (
            id, posting_run_id, project_id, storage_key, original_filename, title,
            content_hash, byte_size, status, posted_url, post_id, posted_at,
            attempts, last_error, created_at, updated_at, credential_id, site_id
        )
        SELECT id, posting_run_id, project_id, storage_key, original_filename, title,
            content_hash, byte_size, status, posted_url, post_id, posted_at,
            attempts, last_error, created_at, updated_at, credential_id, site_id
        FROM text_items_legacy
    """)

    # 8. Снести legacy
    op.execute("DROP TABLE text_items_legacy")


def downgrade() -> None:
    # Обратно в обычную таблицу (для отката; данные сохраняем).
    op.execute("ALTER TABLE text_items RENAME TO text_items_part")
    op.execute("ALTER SEQUENCE text_items_id_seq OWNED BY NONE")
    op.execute("""
        CREATE TABLE text_items (
            id integer NOT NULL DEFAULT nextval('text_items_id_seq'),
            posting_run_id integer NOT NULL,
            project_id integer NOT NULL,
            storage_key varchar(500),
            original_filename varchar(500) NOT NULL,
            title varchar(1000),
            content_hash char(64) NOT NULL,
            byte_size integer NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'pending',
            posted_url text,
            post_id bigint,
            posted_at timestamptz,
            attempts integer NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            credential_id integer,
            site_id integer,
            CONSTRAINT text_items_pkey PRIMARY KEY (id)
        )
    """)
    op.execute("ALTER SEQUENCE text_items_id_seq OWNED BY text_items.id")
    op.execute("""INSERT INTO text_items SELECT
        id, posting_run_id, project_id, storage_key, original_filename, title,
        content_hash, byte_size, status, posted_url, post_id, posted_at,
        attempts, last_error, created_at, updated_at, credential_id, site_id
        FROM text_items_part""")
    op.execute("DROP TABLE text_items_part")
    op.execute("CREATE INDEX ix_text_items_content_hash ON text_items (content_hash)")
    op.execute("CREATE INDEX ix_text_items_credential_id ON text_items (credential_id)")
    op.execute("CREATE INDEX ix_text_items_posting_run_id ON text_items (posting_run_id)")
    op.execute("CREATE INDEX ix_text_items_project_id ON text_items (project_id)")
    op.execute("CREATE INDEX ix_text_items_project_status ON text_items (project_id, status)")
    op.execute("CREATE INDEX ix_text_items_run_status ON text_items (posting_run_id, status)")
    op.execute("CREATE INDEX ix_text_items_site_id ON text_items (site_id)")
