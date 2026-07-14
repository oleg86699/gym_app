export interface GroupBrief {
  id: number
  name: string
}

export interface RoleBrief {
  id: number
  name: string
  is_system: boolean
}

export interface MeResponse {
  id: number
  username: string
  email: string | null
  full_name: string | null
  is_active: boolean
  is_super_admin: boolean
  last_login_at: string | null
  created_at: string
  group: GroupBrief | null
  roles: RoleBrief[]
  permissions: string[]
  accessible_pages: string[]
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: MeResponse
}

export interface User {
  id: number
  username: string
  email: string | null
  full_name: string | null
  is_active: boolean
  last_login_at: string | null
  created_at: string
  group: GroupBrief | null
  roles: RoleBrief[]
}

export interface ProjectBrief {
  id: number
  name: string
  is_active: boolean
}

export interface UserDetail extends User {
  shared_projects: ProjectBrief[]
  direct_page_ids: number[]
  // tag-access RBAC: персональный allowlist батч-тегов. null = без ограничения.
  allowed_tags: string[] | null
}

export interface ProjectChip {
  id: number
  name: string
}

export interface GroupListItem extends Group {
  members_count: number
  owned_projects: ProjectChip[]
  shared_projects: ProjectChip[]
}

export interface UserBriefMin {
  id: number
  username: string
  full_name?: string | null
}

export interface Invitation {
  id: number
  token_prefix: string
  created_by: UserBriefMin | null
  group: GroupBrief | null
  role_ids: number[]
  email: string | null
  note: string | null
  expires_at: string
  is_revoked: boolean
  used_at: string | null
  used_by: UserBriefMin | null
  created_at: string
}

export interface CreatedInvitation extends Invitation {
  plain_token: string
  invite_url: string
}

export interface PublicInvitationView {
  group_name: string | null
  invited_by_username: string | null
  email: string | null
  expires_at: string
}

export interface WpCredential {
  id: number
  site_id: number
  login: string
  password?: string | null  // populated only when fetched with include_password (super_admin)
  is_valid: boolean
  last_validation_kind?: string | null
  last_error_message?: string | null
  can_xmlrpc?: boolean | null
  can_post_via_xmlrpc?: boolean | null
  can_admin_login?: boolean | null
  can_create_users?: boolean | null
  admin_role?: string | null
  provisioned?: boolean
  provisioned_at?: string | null
  provisioned_via?: string | null
  error_counter: number
  last_validated_at: string | null
  amount_use: number
  last_used_at: string | null
  tags: string[] | null
  note: string | null
  source_filename: string | null
  import_batch_id: number | null
  created_at: string
}

export interface WpSite {
  id: number
  domain: string
  hint_path: string | null
  hint_port: number | null
  last_working_url: string | null
  last_working_at: string | null
  is_active: boolean
  language: string | null
  language_detected_at?: string | null
  note: string | null
  created_at: string
  consecutive_site_failures?: number
  last_site_failure_at?: string | null
  last_site_failure_kind?: string | null
  auto_disabled_at?: string | null
}

export interface WpSiteListItem extends WpSite {
  credentials_total: number
  credentials_valid: number
  credentials_invalid: number
  credentials_pending: number
  credentials_transient: number
  credentials_provisioned?: number
  site_can_xmlrpc?: boolean | null
  site_can_post_via_xmlrpc?: boolean | null
  site_can_admin?: boolean | null
  last_credential_check_at?: string | null
  total_uses: number
  last_used_at?: string | null
}

export interface WpSiteDetail extends WpSite {
  credentials: WpCredential[]
}

export interface WpSiteList {
  items: WpSiteListItem[]
  next_cursor: string | null
  has_more: boolean
  total: number
}

export interface SiteEvent {
  id: number
  source: 'validation' | 'posting'
  error_kind: string
  error_message: string | null
  credential_id: number | null
  posting_run_id: number | null
  proxy_id: number | null
  created_at: string | null
}

export interface SitePostEntry {
  text_item_id: number
  posting_run_id: number
  run_name: string
  run_creator_id: number | null
  run_creator_username: string | null
  project_id: number
  project_name: string
  credential_id: number | null
  credential_login: string | null
  posted_url: string | null
  posted_at: string | null
  text_title: string | null
}

export interface SiteAnalytics {
  site_id: number
  domain: string
  posts_total: number
  posts_24h: number
  posts_7d: number
  first_posted_at: string | null
  last_posted_at: string | null
  distinct_projects: number
  distinct_credentials_used: number
  recent_posts: SitePostEntry[]
}

export interface WpPoolSummary {
  sites_total: number
  sites_active: number
  sites_usable?: number
  sites_unusable?: number
  credentials_valid_rpc?: number
  credentials_valid_admin?: number
  credentials_total: number
  credentials_valid: number
  credentials_invalid: number
  credentials_pending: number
  credentials_transient: number
}

export interface WpImportResult {
  imported_credentials: number
  skipped_duplicate_credentials: number
  skipped_invalid_rows: number
  total_rows: number
  sites_created: number
  sites_touched: number
}

export interface DashboardCards {
  active_runs: number
  pending_texts: number
  posts_24h: number
  failed_24h: number
  wp_sites_active: number
  wp_credentials_valid: number
}

export interface DashboardRun {
  id: number
  name: string
  status: PostingRunStatus
  project: { id: number; name: string; is_active?: boolean }
  creator: { id: number; username: string; full_name: string | null } | null
  total_texts: number
  posted_count: number
  failed_count: number
  skipped_count: number
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface AuditEntry {
  id: number
  action: string
  resource_type: string | null
  resource_id: number | null
  changes: Record<string, unknown> | null
  ip: string | null
  user_agent: string | null
  created_at: string
  actor: { id: number; username: string; full_name: string | null } | null
}

export interface AuditListResponse {
  items: AuditEntry[]
  has_more: boolean
}

export interface DashboardData {
  scope: 'all' | 'limited'
  cards: DashboardCards
  active_runs: DashboardRun[]
  recent_runs: DashboardRun[]
}

// ─── WP Import Batches ───────────────────────────────────────────────

export type WpBatchStatus = 'uploaded' | 'queued' | 'validating' | 'paused' | 'done'

export interface WpBatch {
  id: number
  name: string
  tag: string | null
  note: string | null
  cost_total: number | null
  cost_currency: string | null
  source_filename: string | null
  status: WpBatchStatus
  total_credentials: number
  duplicate_credentials: number
  valid_count: number
  valid_xmlrpc_count?: number
  valid_admin_count?: number
  invalid_count: number
  transient_count: number
  pending_count: number
  provisioned_count?: number
  pause_requested: boolean
  validation_started_at: string | null
  validation_finished_at: string | null
  created_by_user_id: number | null
  created_at: string
}

export interface WpBatchImportResult {
  batch_id: number
  parsed_rows: number
  sites_created: number
  sites_touched: number
  credentials_new: number
  credentials_duplicate: number
  skipped_invalid_rows: number
  validation_started: boolean
}

export interface BatchCredEntry {
  id: number
  site_id: number
  domain: string
  language: string | null
  login: string
  password?: string | null    // расшифрован только для super_admin + явного запроса
  tags: string[] | null
  is_valid: boolean
  last_validated_at: string | null
  last_validation_kind: string | null
  last_error_message: string | null
  error_counter: number
  last_error_at: string | null
  error_cooldown_until: string | null
  last_used_at: string | null
  amount_use: number
  created_at: string
  // Capability matrix (Tier 1+2 discovery)
  can_xmlrpc?: boolean | null
  can_admin_login?: boolean | null
  can_post_via_xmlrpc?: boolean | null
  can_post_via_admin?: boolean | null
  admin_role?: string | null
  can_create_users?: boolean | null
  last_admin_check_at?: string | null
  provisioned?: boolean
  provisioned_at?: string | null
  provisioned_via?: string | null
  provisioned_here?: boolean
  import_batch_id?: number | null
  language_detected_at?: string | null
}

export interface BatchCredListResponse {
  items: BatchCredEntry[]
  has_more: boolean
}

// ─── Proxies ──────────────────────────────────────────────────────────

export interface Proxy {
  id: number
  protocol: string
  host: string
  port: number
  username: string | null
  country: string | null
  provider: string | null
  proxy_type: string | null
  note: string | null
  is_active: boolean
  status: string
  last_checked_at: string | null
  last_check_error: string | null
  external_ip: string | null
  isp: string | null
  asn: string | null
  source: string
  source_id: string | null
  created_at: string
}

export interface ProxyListResponse {
  items: Proxy[]
  total: number
  next_cursor: string | null
  has_more: boolean
}

export interface ProxyProviderStat {
  source: string
  count: number
}

export interface ProxySourceField {
  name: string
  label: string
  type: 'text' | 'password' | 'number' | 'textarea' | 'select'
  required?: boolean
  default?: string | null
  placeholder?: string | null
  help?: string | null
  options?: string[] | null
}

export interface ProxySourceMeta {
  name: string
  display_name: string
  fields: ProxySourceField[]
}

export interface ProxyCheckResult {
  ok: boolean
  external_ip: string | null
  country: string | null
  isp: string | null
  asn: string | null
  proxy_type: string | null
  error: string | null
}

export interface ProxyBulkResult {
  parsed: number
  inserted: number
  invalid: string[]
}

export interface ProxyImportResult {
  created: number
  updated: number
  total_in_db: number
}

export interface WpValidationState {
  running: boolean
  scope: 'all' | 'invalid' | 'stale'
  started_at: string | null
  finished_at: string | null
  total: number
  done: number
  valid: number
  invalid: number
  transient_errors: number
  last_actor_id: number | null
}

// ─── Postings ────────────────────────────────────────────────────────

export type PostingRunStatus =
  | 'draft'
  | 'unpacking'
  | 'ready'
  | 'scheduled'
  | 'queued'
  | 'running'
  | 'paused'
  | 'done'
  | 'failed'
  | 'need_more_admins'
  | 'cancelled'
  | 'interrupted'
  | 'needs_review'

export type PostingRunPriority = 'low' | 'normal' | 'high'

export interface PostingRun {
  id: number
  project: { id: number; name: string }
  creator: { id: number; username: string; full_name: string | null } | null
  deleted_at?: string | null   // two-level delete: soft-deleted
  deleted_by?: number | null   // кто скрыл (super-аудит)
  deleted_by_user?: { id: number; username: string; full_name: string | null } | null
  name: string
  status: PostingRunStatus
  task_type?: 'post' | 'sitewide_link' | 'homepage_link'
  content_source?: 'upload_txt' | 'csv_direct' | 'csv_campaign' | 'spin_fanout'
  content_mode?: string | null
  run_mode?: 'auto' | 'manual'
  priority: PostingRunPriority
  posting_method?: 'auto' | 'xmlrpc_only' | 'admin_only'
  proxy_fallback_direct?: boolean
  post_verify?: 'mark' | 'auto'
  spread_days?: number
  content_params?: { language: string | null; model: string | null; prompt: string | null; error?: string | null } | null
  scheduled_for: string | null
  publish_from: string | null  // ISO date "YYYY-MM-DD"
  publish_to: string | null
  // Фильтр пула доступов (из gen_params) — пусто всё = весь пул
  site_langs?: string[] | null
  site_tlds?: string[] | null
  site_tags?: string[] | null
  site_domains_count?: number | null
  site_domains_file?: boolean
  concurrency: number
  timeout_seconds: number
  max_posts_per_site: number   // сколько раз один сайт можно использовать в задаче (1 = «1 сайт = 1 пост»)
  proxy_selector?: string | null
  pool_fallback?: boolean
  pause_requested: boolean
  cancel_requested: boolean
  total_texts: number
  posted_count: number
  failed_count: number
  skipped_count: number
  gen_done: number | null
  gen_total: number | null
  fillable_spins?: number | null   // gen_per_row: пустых спинов с готовым оригиналом
  last_progress_at: string | null
  started_at: string | null
  finished_at: string | null
  worker_heartbeat_at: string | null
  // Перепроверка проставленных ссылок (link-check): статус и прогресс.
  link_check_status: 'queued' | 'running' | 'done' | null
  link_check_total: number
  link_check_done: number
  link_check_valid: number
  link_check_at: string | null
  source_archive_storage_key: string | null
  created_at: string
}

export interface CreateRunParams {
  name: string
  priority?: PostingRunPriority
  max_posts_per_site?: number            // 1 = «1 сайт = 1 пост»; подними чтобы добрать сайты
  scheduled_for?: string | null
  // Окно публикации этого прогона (YYYY-MM-DD). Обе пустые → глобальный дефолт из settings
  publish_from?: string | null
  publish_to?: string | null
  spread_days?: number                   // drip-feed: размазать постинг на N дней (0 = сразу)
  proxy_id?: number | null              // legacy, оставлено для back-compat
  proxy_selector?: string | null         // "direct" | "all" | "provider:<name>" | "single:<id>"
  posting_method?: 'auto' | 'xmlrpc_only' | 'admin_only'
  // Фильтр пула сайтов (через запятую): lang "en,fr", tld "us,uk"
  site_langs?: string | null
  site_tlds?: string | null
  // Пул доступов: по тегам кредов (через запятую) или свой список доменов
  // (через запятую/перенос). Пусто = весь пул.
  site_tags?: string | null
  site_domains?: string | null
  site_domains_key?: string | null  // большой список доменов файлом в MinIO
  // csv_direct: инжектить ли ссылку из строки в тело (по умолчанию false)
  csv_inject_link?: boolean
  // ─── csv_campaign (Content Engine) ───
  content_mode?: 'gen_per_post' | 'gen_per_row' | 'reuse'
  run_mode?: 'auto' | 'manual'
  prompt_template_id?: number | null
  ai_model_id?: number | null
  language?: string | null
}

// ─── AI Settings (C2) ───
export type AiProviderType = 'openai' | 'anthropic' | 'google'
export type AiModelPurpose = 'content' | 'spin' | 'any'

export interface AiModel {
  id: number
  provider_id: number
  display_name: string
  model_id: string
  temperature: number
  max_tokens: number
  purpose: AiModelPurpose
  is_active: boolean
  created_at: string
}

export interface AiProvider {
  id: number
  name: string
  type: AiProviderType
  base_url: string | null
  is_active: boolean
  created_at: string
  has_key: boolean
  models: AiModel[]
}

export interface PromptTemplate {
  id: number
  name: string
  body: string
  notes: string | null
  created_at: string
}

export type TextItemStatus =
  | 'pending' | 'generating' | 'posting' | 'posted' | 'failed' | 'skipped' | 'needs_review'

export interface LinkCandidate {
  link: string
  anchor: string
  domain: string | null
  is_project_domain: boolean
}

export interface TextItem {
  id: number
  status: TextItemStatus
  title: string | null
  original_filename: string
  byte_size: number
  text_id: number | null
  attempts: number
  last_error: string | null
  posted_url: string | null
  post_id: number | null
  posted_at: string | null
  created_at: string
  site: { id: number; domain: string } | null
  credential: { id: number; login: string } | null
  // link-типы (sitewide/homepage)
  link_url?: string | null
  link_anchor?: string | null
  placed_via?: string | null
  verified_at?: string | null
  link_verified?: boolean | null   // валидация бэклинка на посте: null/✓/✗
  verify_attempts?: number
  // Фаза A: разбор ссылок + язык
  target_domain?: string | null
  lang?: string | null
  link_candidates?: LinkCandidate[] | null
}

// ─── Домены проекта + аналитика (Фаза A) ─────────────────────────────
export interface ProjectDomain {
  id: number
  domain: string
  created_at: string
  deleted_at?: string | null
  deleted_by?: number | null
  deleted_by_user?: { id: number; username: string; full_name: string | null } | null
}
export interface AddDomainResult {
  domain: string
  created: boolean
  auto_resolved_runs: number
}
export interface BulkAddDomainsResult {
  added: string[]
  duplicates: string[]
  invalid: string[]
  auto_resolved_runs: number
}
export interface DomainAnalyticsRow {
  target_domain: string
  total: number
  posted: number
}

export interface DomainSummary {
  domain: string
  total: number
  posted: number
  failed: number
  skipped: number
  in_progress: number
  sites: number
  runs: number
  last_posted_at: string | null
  available_sites: number   // свободные уникальные сайты под этот домен (из пула)
  pool_total: number        // всего постабельных сайтов в пуле
}

export interface DomainItemRow {
  id: number
  status: TextItemStatus
  link_url: string | null
  link_anchor: string | null
  posted_url: string | null
  posted_at: string | null
  last_error: string | null
  run_id: number
  run_name: string | null
  site_domain: string | null
}

export interface DomainItemsResponse {
  items: DomainItemRow[]
  next_cursor: number | null
  has_more: boolean
}

export interface DomainRunRow {
  id: number
  name: string
  status: PostingRunStatus
  task_type?: 'post' | 'sitewide_link' | 'homepage_link'
  content_source?: string | null
  content_mode?: string | null
  run_mode?: 'auto' | 'manual'
  scheduled_for: string | null
  created_at: string
  total: number
  posted: number
  failed: number
}

export interface DomainPlacement {
  posted_url: string | null
  link_url: string | null
  anchor: string
  verified: boolean | null
  posted_at: string | null
  type: 'post' | 'sitewide_link' | 'homepage_link'
}

// ─── Content Engine: spin-fanout (C1) ────────────────────────────────
export interface SpinOriginalInput {
  spintax: string
  title?: string | null
  lang?: string | null
}
export interface SpinPlacementRow {
  link: string
  anchor?: string
  count?: number
}
export interface CreateSpinRunParams {
  name: string
  originals: SpinOriginalInput[]
  rows: SpinPlacementRow[]
  run_mode?: 'auto' | 'manual'
  priority?: PostingRunPriority
  scheduled_for?: string | null
  spread_days?: number
  proxy_selector?: string | null
  posting_method?: 'auto' | 'xmlrpc_only' | 'admin_only'
}
export interface SpinOriginalRow {
  id: number
  title: string | null
  lang: string | null
  spintax: string
  link: string | null
  anchor: string | null
  placements: number
}

// ─── Библиотека текстов (B2: поиск) ──────────────────────────────────
export interface TextSearchRow {
  id: number
  title: string | null
  lang: string | null
  source: string
  gen_model: string | null
  content_hash: string
  times_used: number
  posted_count: number
  item_count: number
  created_at: string
  last_used_at: string | null
  snippet: string | null
  rank: number
  reusable: boolean
  has_spin: boolean
  used_as_original: boolean
  parent_text_id: number | null
  spin_count: number
  anchor: string | null
  keyword: string | null
  link: string | null
}

export interface TextDetail {
  id: number
  title: string | null
  body: string
  lang: string | null
  source: string
  reusable: boolean
  spin_formula: string | null
  parent_text_id: number | null
  times_used: number
  created_at: string
}

export interface RunProgress {
  total: number
  pending: number
  generating: number
  posting: number
  posted: number
  failed: number
  skipped: number
  needs_review: number
  generated: number   // айтемы с готовым текстом (для dual-бара ген/пост)
}

export interface TextItemDetail extends TextItem {
  posting_run_id: number
  project_id: number | null
  content: string
  editable: boolean
}

export interface AppSettings {
  default_concurrency: number
  default_timeout_seconds: number
  global_posting_concurrency: number
  cf_browser_concurrency: number
  posting_concurrency_floor: number
  site_disable_threshold: number
  site_disable_threshold_cf: number
  max_concurrent_batch_validations: number
  max_concurrent_link_checks: number
  default_publish_from: string | null  // ISO date "YYYY-MM-DD"
  default_publish_to: string | null
  limits: {
    min_concurrency: number
    max_concurrency: number
    min_timeout_seconds: number
    max_timeout_seconds: number
    min_global_posting_concurrency: number
    max_global_posting_concurrency: number
    min_cf_browser_concurrency: number
    max_cf_browser_concurrency: number
    min_concurrency_floor: number
    max_concurrency_floor: number
    min_site_disable_threshold: number
    max_site_disable_threshold: number
    min_site_disable_threshold_cf: number
    max_site_disable_threshold_cf: number
    min_max_concurrent_batch_validations: number
    max_max_concurrent_batch_validations: number
    min_max_concurrent_link_checks: number
    max_max_concurrent_link_checks: number
  }
}

// ─── Global Queue (унифицированный снапшот всех полос работы) ─────────
export interface QueueLimiter {
  name: string
  in_use: number
  limit: number
  throttled: boolean
  utilization_pct: number
}
export interface QueuePostingItem {
  id: number
  name: string
  project_id: number
  task_type: 'post' | 'sitewide_link' | 'homepage_link'
  status: PostingRunStatus
  total: number
  posted: number
  failed: number
  skipped: number
  progress_pct: number
  gen_done: number | null   // прогресс AI-генерации (csv_campaign), пока идёт — красный бар
  gen_total: number | null
  generated: number         // айтемы с текстом — для dual-бара (зелёный пост / красный ген)
  started_at: string | null
  last_progress_at: string | null
  heartbeat_at: string | null
  scheduled_for: string | null
}
export interface QueueValidation {
  running: boolean
  scope: string
  total: number
  done: number
  valid: number
  invalid: number
  transient_errors: number
  progress_pct: number
  started_at: string | null
  finished_at: string | null
}
export interface QueueLinkCheck {
  id: number
  name: string
  project_id: number
  status: 'queued' | 'running'
  total: number
  done: number
  valid: number
  progress_pct: number
  started_at: string | null
}
export interface GlobalQueueSnapshot {
  limiter: QueueLimiter
  posting: QueuePostingItem[]
  validation: QueueValidation | null
  link_checks: QueueLinkCheck[]
  summary: {
    posting_active: number
    posting_running: number
    validation_running: boolean
    link_check_active: number
  }
}

export interface Paginated<T> {
  items: T[]
  next_cursor: string | null
  has_more: boolean
}

// Глобальная очередь /postings/queue — видна всем, чтобы юзеры видели «где их задача».
export interface QueueItem {
  id: number
  name: string
  status: PostingRunStatus
  priority: PostingRunPriority
  project_name: string
  creator_username: string | null
  total_texts: number
  posted_count: number
  failed_count: number
  gen_done: number | null
  gen_total: number | null
  scheduled_for: string | null
  started_at: string | null
  created_at: string
  is_mine: boolean
}

export interface QueueLinkCheckItem {
  id: number
  name: string
  project_name: string
  creator_username: string | null
  status: 'queued' | 'running'
  total: number
  done: number
  valid: number
  is_mine: boolean
}
export interface QueueResponse {
  items: QueueItem[]
  total: number
  link_checks: QueueLinkCheckItem[]
}

export interface Group {
  id: number
  name: string
  description: string | null
  is_active: boolean
  created_at: string
  // tag-access RBAC: потолок разрешённых команде батч-тегов. null = все теги.
  allowed_tags: string[] | null
}

export interface Permission {
  id: number
  code: string
  resource: string
  action: string
  description: string | null
}

export interface PageBrief {
  id: number
  path: string
  name: string
  is_active: boolean
}

export interface Role {
  id: number
  name: string
  description: string | null
  is_active: boolean
  is_system: boolean
  is_assignable_by_group_admin: boolean
  permissions: Permission[]
  pages: PageBrief[]
}

export interface AdminPage {
  id: number
  path: string
  name: string
  description: string | null
  is_active: boolean
  created_at: string
  role_ids: number[]
  user_ids: number[]
}

export interface UserBrief {
  id: number
  username: string
  full_name: string | null
}

export interface Project {
  id: number
  name: string
  description: string | null
  is_active: boolean
  created_at: string
  deleted_at?: string | null   // two-level delete: soft-deleted
  deleted_by?: number | null   // кто скрыл (super-аудит)
  deleted_by_user?: { id: number; username: string; full_name: string | null } | null
  owner: UserBrief
  owner_group: GroupBrief | null
  shared_with_users: UserBrief[]
  shared_with_groups: GroupBrief[]
}

// List-эндпоинт возвращает расширенный объект — с live-метриками.
export interface ProjectListItem extends Project {
  active_runs: number          // queued/running/paused/scheduled/unpacking
  failed_runs: number          // failed/need_more_admins/interrupted
  runs_total: number
  posted_total: number
  posted_24h: number
  last_activity_at: string | null
  available_admins: number     // постабельных сайтов (valid admin + рабочий канал), ещё не использованных в проекте
  valid_admins_pool: number    // всего постабельных сайтов в пуле (контекст X/Y) — = пулу _pick_candidate_sites
}
