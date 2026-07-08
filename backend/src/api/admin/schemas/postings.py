"""Pydantic-схемы для /admin/api/postings."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    full_name: str | None = None


class ProjectBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class ContentParamsBrief(BaseModel):
    """Параметры генерации csv_campaign-рана для шапки (имена, не id)."""
    language: str | None = None
    model: str | None = None
    prompt: str | None = None
    error: str | None = None   # причина FAILED (генерация), если была


class PostingRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project: ProjectBrief
    creator: UserBrief | None
    deleted_at: datetime | None = None   # two-level delete: soft-deleted
    deleted_by: int | None = None        # admin_user.id, кто скрыл (super-аудит)
    deleted_by_user: UserBrief | None = None  # кто скрыл (для показа @username)

    name: str
    status: str
    task_type: str = "post"  # post | sitewide_link | homepage_link
    content_source: str = "upload_txt"  # upload_txt | csv_direct | csv_campaign | spin_fanout
    content_mode: str | None = None
    run_mode: str = "auto"  # auto | manual
    priority: str  # low | normal | high
    posting_method: str = "auto"  # auto | xmlrpc_only | admin_only
    # Пре-флайт прокси: пул оказался мёртв на старте → постинг ушёл в direct.
    proxy_fallback_direct: bool = False
    post_verify: str = "mark"  # mark | auto — валидация ссылки на посте
    # drip-feed: на сколько дней размазан постинг (0 = сразу)
    spread_days: int = 0
    # параметры генерации (только для csv_campaign в детальном ответе)
    content_params: ContentParamsBrief | None = None
    # Фильтр пула доступов (из gen_params) — для инфо в UI. Пусто всё = весь пул.
    site_langs: list[str] | None = None
    site_tlds: list[str] | None = None
    site_tags: list[str] | None = None
    site_domains_count: int | None = None   # inline-список доменов: сколько
    site_domains_file: bool = False          # домены загружены файлом (MinIO)

    scheduled_for: datetime | None
    # Окно публикации (для аудита; берётся из app_settings при создании прогона).
    publish_from: date | None
    publish_to: date | None
    # Concurrency/timeout читаются из app_settings при создании, но сохраняются
    # на run для аудита: с какими параметрами реально гнали.
    concurrency: int
    timeout_seconds: int
    # Лимит повторного использования сайта в этой задаче (1 = «1 сайт = 1 пост»)
    max_posts_per_site: int = 1
    # Селектор прокси-пула (direct / all / provider:<name> / single:<id>) — для
    # префилла в форме правки.
    proxy_selector: str | None = None
    # pool_fallback: добить по полному разрешённому пулу при исчерпании фильтра.
    pool_fallback: bool = False

    pause_requested: bool
    cancel_requested: bool

    total_texts: int
    posted_count: int
    failed_count: int
    skipped_count: int
    # Прогресс генерации (csv_campaign в фазе UNPACKING) — для красного бара.
    gen_done: int | None = None
    gen_total: int | None = None
    # gen_per_row: число пустых спин-айтемов с готовым оригиналом — кнопка
    # «Заполнить спины» (расшить без старта постинга). 0/None → кнопки нет.
    fillable_spins: int | None = None

    last_progress_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    worker_heartbeat_at: datetime | None

    # Перепроверка проставленных ссылок (link-check) — фоновая задача после
    # завершения постинга. status: NULL|queued|running|done.
    link_check_status: str | None = None
    link_check_total: int = 0
    link_check_done: int = 0
    link_check_valid: int = 0
    link_check_at: datetime | None = None

    source_archive_storage_key: str | None

    created_at: datetime


class QueueItem(BaseModel):
    """Минималистичная схема для глобальной очереди — что видят все юзеры.
    Только то что нужно чтобы понимать «есть ли передо мной задачи»."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    priority: str
    project_name: str
    creator_username: str | None
    total_texts: int
    posted_count: int
    failed_count: int
    # Прогресс генерации (UNPACKING) — красный бар генерации в очереди.
    gen_done: int | None = None
    gen_total: int | None = None
    scheduled_for: datetime | None
    started_at: datetime | None
    created_at: datetime
    is_mine: bool = False    # выставляет endpoint в зависимости от viewer-а


class QueueLinkCheckItem(BaseModel):
    """Активная перепроверка проставленных ссылок (link-check) — отдельный
    (фиолетовый) тип нагрузки, чтобы очередь не выглядела пустой во время неё."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    project_name: str
    creator_username: str | None = None
    status: str  # queued | running
    total: int
    done: int
    valid: int
    is_mine: bool = False


class QueueResponse(BaseModel):
    items: list[QueueItem]
    total: int
    link_checks: list[QueueLinkCheckItem] = []


class UpdateRunParams(BaseModel):
    """Редактирование задачи после создания. `max_posts_per_site` — в любом
    статусе (воркер читает live). Остальные постинг-параметры — только пока
    задача ещё НЕ начала постить (READY / SCHEDULED). Все поля опциональны:
    меняем ТОЛЬКО явно переданные (определяем по model_fields_set), поэтому
    None у scheduled_for/publish_* трактуется как «очистить»."""
    max_posts_per_site: int | None = Field(default=None, ge=1, le=1000)
    priority: str | None = Field(default=None, pattern="^(low|normal|high)$")
    scheduled_for: datetime | None = None
    spread_days: int | None = Field(default=None, ge=0, le=365)
    posting_method: str | None = Field(default=None, pattern="^(auto|xmlrpc_only|admin_only)$")
    post_verify: str | None = Field(default=None, pattern="^(mark|auto)$")
    proxy_selector: str | None = Field(default=None, max_length=120)
    publish_from: date | None = None
    publish_to: date | None = None
    site_langs: str | None = Field(default=None, max_length=200)
    site_tlds: str | None = Field(default=None, max_length=200)
    site_tags: str | None = Field(default=None, max_length=2000)
    site_domains: str | None = Field(default=None, max_length=200_000)
    site_domains_key: str | None = Field(default=None, max_length=300)
    pool_fallback: bool | None = None


class CreateRunParams(BaseModel):
    """JSON-параметры прогона при создании. Сам файл — отдельный multipart field.

    Concurrency, timeout и окно публикации (publish_from/to) НЕ задаются
    менеджером — общие для системы, редактируются super_admin-ом через
    /admin/api/app-settings.
    """

    name: str = Field(min_length=1, max_length=255)
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")
    # Сколько раз один WP-сайт можно использовать в ЭТОЙ задаче. 1 = «1 сайт =
    # 1 пост» (защита от спама). Подними, если не хватает доступов и хочешь
    # добрать из уже использованных сайтов. Редактируется и после создания.
    max_posts_per_site: int = Field(default=1, ge=1, le=1000)
    scheduled_for: datetime | None = None
    # Окно публикации [publish_from, publish_to] для ЭТОГО прогона: воркер ставит
    # каждому посту случайную (прошедшую) дату внутри окна. Если обе пустые —
    # берётся глобальный дефолт из app_settings (default_publish_from/to).
    publish_from: date | None = None
    publish_to: date | None = None
    # Drip-feed: размазать постинг всех текстов на N дней (link velocity).
    # 0 = постить всё сразу. Окно стартует от scheduled_for (или момента запуска).
    spread_days: int = Field(default=0, ge=0, le=365)
    # DEPRECATED: одиночный прокси. Оставлено для back-compat.
    # Использовать proxy_selector — он умеет пулы.
    proxy_id: int | None = None
    # Селектор прокси-пула:
    #   "direct"            — без прокси
    #   "all"               — все active+working прокси, round-robin per request
    #   "provider:<name>"   — все active прокси конкретного провайдера
    #   "single:<proxy_id>" — один конкретный proxy (старое поведение)
    # Если None — fallback на proxy_id (back-compat).
    proxy_selector: str | None = Field(default=None, max_length=120)
    # Канал постинга:
    #   auto (default) — XML-RPC сначала, fallback на wp-admin при отключённом RPC
    #   xmlrpc_only — только XML-RPC, классический подход. Самый дешёвый.
    #   admin_only — только form-login через wp-admin. Дороже, но обходит сайты
    #                 где XML-RPC выключен плагинами Disable XML-RPC.
    posting_method: str = Field(default="auto", pattern="^(auto|xmlrpc_only|admin_only)$")
    # ─── Валидация бэклинка на опубликованном посте (post-типы) ───
    #   mark — после поста 1 GET, отметка ✓/✗ + резолвленный permalink (пост done в любом случае).
    #   auto — перепост на другой сайт, пока ссылка не подтвердится (иначе не done).
    post_verify: str = Field(default="mark", pattern="^(mark|auto)$")
    # ─── Фильтр пула сайтов (несколько значений через запятую) ───
    # Берём из wp-sites только сайты с language ∈ site_langs и доменом,
    # оканчивающимся на один из site_tlds. Пусто = без ограничения.
    #   site_langs="en,fr,de"   site_tlds="us,uk,au"
    site_langs: str | None = Field(default=None, max_length=200)
    site_tlds: str | None = Field(default=None, max_length=200)
    # Пул доступов по тегам кредов (через запятую). Пусто = весь пул. Берём
    # только сайты, у которых есть валидный постабельный cred с одним из тегов.
    site_tags: str | None = Field(default=None, max_length=2000)
    # Свой список доменов (через запятую/перенос строки): постим ТОЛЬКО на эти
    # домены, креды к ним берём из базы. Пусто = без ограничения по домену.
    site_domains: str | None = Field(default=None, max_length=200_000)
    # Большой список доменов — загружен файлом в MinIO (см. /postings/domain-list);
    # тут лежит ключ объекта. Приоритет у inline site_domains, если задан.
    site_domains_key: str | None = Field(default=None, max_length=300)
    # pool_fallback: при исчерпании фильтрованного пула — добить по всему
    # остальному разрешённому пулу вместо need_more_admins.
    pool_fallback: bool = False
    # CSV-direct: инжектить ли ссылку из колонки link в тело (колонку text).
    # False (default) — тело постится как есть (ссылка должна быть уже в тексте).
    # True → применяем inject_link(body, link, anchor), как в Reuse-пути.
    csv_inject_link: bool = False

    # ─── csv_campaign (Content Engine): режим контента + AI ───
    # gen_per_post — уникальный текст на каждый пост; gen_per_row — 1 текст/строку
    # + спин-расшивка на count; reuse — переиспользование reusable-оригиналов.
    content_mode: str | None = Field(default=None, pattern="^(gen_per_post|gen_per_row|reuse)$")
    # auto — после генерации сразу в очередь постинга; manual — READY (ревью → Start).
    run_mode: str = Field(default="manual", pattern="^(auto|manual)$")
    prompt_template_id: int | None = None
    ai_model_id: int | None = None
    language: str | None = Field(default=None, max_length=10)


class LinkRow(BaseModel):
    url: str = Field(min_length=4, max_length=2000)
    anchor: str = Field(default="", max_length=500)


# ─── Content Engine: ручной spin_fanout (C1) ──────────────────────────
class SpinOriginal(BaseModel):
    spintax: str = Field(min_length=1, max_length=1_000_000)
    title: str | None = Field(default=None, max_length=1000)
    lang: str | None = Field(default=None, max_length=10)


class SpinPlacementRow(BaseModel):
    link: str = Field(min_length=4, max_length=2000)
    anchor: str = Field(default="", max_length=500)
    count: int = Field(default=1, ge=1, le=100000)


class CreateSpinRunParams(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    originals: list[SpinOriginal] = Field(min_length=1, max_length=200)
    rows: list[SpinPlacementRow] = Field(min_length=1, max_length=100000)
    run_mode: str = Field(default="manual", pattern="^(auto|manual)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")
    scheduled_for: datetime | None = None
    spread_days: int = Field(default=0, ge=0, le=365)
    proxy_selector: str | None = Field(default=None, max_length=120)
    posting_method: str = Field(default="auto", pattern="^(auto|xmlrpc_only|admin_only)$")
    site_langs: str | None = Field(default=None, max_length=200)
    site_tlds: str | None = Field(default=None, max_length=200)


class SpinOriginalRow(BaseModel):
    """Оригинал для ревью (texts-строка) + куда он расшьётся на Start."""
    id: int
    title: str | None
    lang: str | None
    spintax: str        # = texts.body (спинтакс)
    # из fanout_groups: целевая ссылка/анкор и на сколько размещений расшьётся
    link: str | None = None
    anchor: str | None = None
    placements: int = 1


class CreateLinkRunParams(BaseModel):
    """Создание link-run-а (сквозная/homepage ссылка). Вместо zip — строки ссылок;
    сайты подбираются из пула администраторов."""

    name: str = Field(min_length=1, max_length=255)
    task_type: str = Field(pattern="^(sitewide_link|homepage_link)$")
    links: list[LinkRow] = Field(min_length=1, max_length=50)
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")
    # ограничить число целевых сайтов (None = все подходящие)
    max_sites: int | None = Field(default=None, ge=1, le=100000)


def parse_site_filter(s: str | None) -> list[str]:
    """'en, FR ,de' → ['en','fr','de']. Пусто → []."""
    if not s:
        return []
    return [x.strip().lower() for x in s.split(",") if x.strip()]


class CreateLinkRunFileParams(BaseModel):
    """Создание link-run-а из файла anchor,link,count. count = на сколько сайтов
    поставить ссылку (per-link). Сами ссылки приходят файлом (csv/xlsx)."""

    name: str = Field(min_length=1, max_length=255)
    task_type: str = Field(pattern="^(sitewide_link|homepage_link)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")
    site_langs: str | None = Field(default=None, max_length=200)
    site_tlds: str | None = Field(default=None, max_length=200)
    # Пул доступов: по тегам кредов / по своему списку доменов (см. CreateRunParams).
    site_tags: str | None = Field(default=None, max_length=2000)
    site_domains: str | None = Field(default=None, max_length=200_000)
    site_domains_key: str | None = Field(default=None, max_length=300)
    # Сколько раз один сайт можно использовать в задаче (1 = «1 сайт = 1 ссылка»).
    max_posts_per_site: int = Field(default=1, ge=1, le=1000)
    # Отложенный старт: пусто = стартует сразу (READY → ручной Start). Задано =
    # SCHEDULED, cron поднимет в назначенное время.
    scheduled_for: datetime | None = None
    # Drip-feed: размазать простановку ссылок на N дней (0 = всё сразу).
    spread_days: int = Field(default=0, ge=0, le=365)
    # Селектор прокси-пула: "direct" | "all" | "provider:<name>" | "single:<id>".
    proxy_selector: str | None = Field(default=None, max_length=120)
    # Окно публикации [publish_from, publish_to] для этого прогона. Обе пустые →
    # глобальный дефолт из app_settings.
    publish_from: date | None = None
    publish_to: date | None = None


# ─── Run detail: progress + text_items ────────────────────────────────


class RunProgressResponse(BaseModel):
    total: int
    pending: int
    generating: int = 0
    posting: int
    posted: int
    failed: int
    skipped: int
    needs_review: int = 0
    generated: int = 0   # айтемы с готовым текстом (для dual-бара ген/пост)


class SiteBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    domain: str


class CredentialBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    login: str


class TextItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    title: str | None
    original_filename: str
    byte_size: int
    # есть ли тело: gen_per_row создаёт пустые item-ы (text_id=NULL) до Start
    text_id: int | None = None
    attempts: int
    last_error: str | None
    posted_url: str | None
    post_id: int | None
    posted_at: datetime | None
    created_at: datetime
    site: SiteBrief | None
    credential: CredentialBrief | None
    # link-типы (sitewide/homepage)
    link_url: str | None = None
    link_anchor: str | None = None
    placed_via: str | None = None
    verified_at: datetime | None = None
    # Валидация бэклинка на посте: NULL=не проверяли, true=ссылка есть, false=нет.
    link_verified: bool | None = None
    verify_attempts: int = 0
    # Фаза A: разбор ссылок + язык
    target_domain: str | None = None
    lang: str | None = None
    link_candidates: list | None = None


class TextItemDetailResponse(TextItemResponse):
    """Полная карточка text_item + raw HTML контент из MinIO + run_id."""

    posting_run_id: int
    project_id: int | None = None  # для «добавить домен в проект» из карточки resolve
    content: str
    editable: bool


class ResolveBulkRequest(BaseModel):
    """Массовый резолв needs_review-задач прогона по одному домену."""

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=255)


class UpdateTextItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=1000)
    # Максимум 1 МБ HTML — больше zip-распаковщик и не пускает (см. unpack.py).
    content: str = Field(..., max_length=1 * 1024 * 1024)
