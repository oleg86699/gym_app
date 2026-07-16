/** Типизированные обёртки над /admin/api/* */

import { api } from './client'
import type {
  AdminPage,
  AiModel,
  AiProvider,
  AiShareRequest,
  PromptTemplate,
  AppSettings,
  AuditListResponse,
  CreatedInvitation,
  DashboardData,
  Group,
  GroupListItem,
  Invitation,
  LoginResponse,
  MeResponse,
  Paginated,
  Permission,
  Project,
  PublicInvitationView,
  Role,
  CreateRunParams,
  CreateSpinRunParams,
  SpinOriginalRow,
  TextSearchRow,
  TextDetail,
  PostingRun,
  Proxy,
  ProxyBulkResult,
  ProxyCheckResult,
  ProxyImportResult,
  ProxyListResponse,
  ProxyProviderStat,
  ProxySourceMeta,
  ProjectDomain,
  AddDomainResult,
  BulkAddDomainsResult,
  DomainAnalyticsRow,
  DomainSummary,
  DomainItemsResponse,
  DomainRunRow,
  DomainPlacement,
  QueueResponse,
  GlobalQueueSnapshot,
  RunProgress,
  BatchCredListResponse,
  WpBatch,
  WpBatchImportResult,
  TextItem,
  TextItemDetail,
  User,
  UserDetail,
  WpCredential,
  WpImportResult,
  WpValidationState,
  WpPoolSummary,
  SiteAnalytics,
  SiteEvent,
  WpSiteDetail,
  WpSiteList,
} from './types'

// ─── Auth ────────────────────────────────────────────────────────────

export const auth = {
  login: (username: string, password: string) =>
    api.post<LoginResponse>('/admin/api/auth/login', { username, password }),
  logout: () => api.post('/admin/api/auth/logout'),
  me: () => api.get<MeResponse>('/admin/api/auth/me'),
  changePassword: (current_password: string, new_password: string) =>
    api.patch('/admin/api/auth/me/password', { current_password, new_password }),
  changeEmail: (current_password: string, new_email: string) =>
    api.patch<MeResponse>('/admin/api/auth/me/email', { current_password, new_email }),
}

// ─── Users ───────────────────────────────────────────────────────────

export const users = {
  list: (query?: { cursor?: string; limit?: number; group_id?: number; search?: string }) =>
    api.get<Paginated<User>>('/admin/api/users', query as Record<string, string | number | undefined>),
  get: (id: number) => api.get<UserDetail>(`/admin/api/users/${id}`),
  create: (payload: {
    username: string
    password: string
    email?: string
    full_name?: string
    group_id?: number
    role_ids?: number[]
    is_active?: boolean
  }) => api.post<User>('/admin/api/users', payload),
  update: (
    id: number,
    payload: {
      username?: string
      full_name?: string
      email?: string
      is_active?: boolean
      password?: string
      group_id?: number | null
      is_remove_from_group?: boolean
      role_ids?: number[]
      project_ids?: number[]
      page_ids?: number[]
      allowed_tags?: string[] | null
    },
  ) => api.patch<User>(`/admin/api/users/${id}`, payload),
  remove: (id: number) => api.del(`/admin/api/users/${id}`),
  resetPassword: (id: number, new_password: string) =>
    api.post(`/admin/api/users/${id}/reset-password`, { new_password }),
}

// ─── Groups ──────────────────────────────────────────────────────────

export const groups = {
  list: () => api.get<GroupListItem[]>('/admin/api/groups'),
  get: (id: number) => api.get<Group>(`/admin/api/groups/${id}`),
  members: (id: number) => api.get<User[]>(`/admin/api/groups/${id}/members`),
  projects: (id: number) => api.get<Project[]>(`/admin/api/groups/${id}/projects`),
  create: (payload: { name: string; description?: string; is_active?: boolean }) =>
    api.post<Group>('/admin/api/groups', payload),
  update: (
    id: number,
    payload: {
      name?: string
      description?: string
      is_active?: boolean
      shared_project_ids?: number[]
      allowed_tags?: string[] | null
    },
  ) => api.patch<Group>(`/admin/api/groups/${id}`, payload),
  remove: (id: number) => api.del(`/admin/api/groups/${id}`),
}

// ─── Projects ────────────────────────────────────────────────────────

export const projects = {
  list: (query?: { cursor?: string; limit?: number; search?: string; owner_id?: number; include_deleted?: boolean }) =>
    api.get<Paginated<Project>>('/admin/api/projects', query as Record<string, string | number | boolean | undefined>),
  get: (id: number) => api.get<Project>(`/admin/api/projects/${id}`),
  // Two-level delete (super_admin only): restore soft-deleted / purge hard
  restore: (id: number) => api.post(`/admin/api/projects/${id}/restore`, {}),
  purge: (id: number) => api.del(`/admin/api/projects/${id}/purge`),
  reassignOwner: (id: number, new_owner_id: number) =>
    api.post<Project>(`/admin/api/projects/${id}/reassign-owner`, { new_owner_id }),
  create: (payload: { name: string; description?: string }) =>
    api.post<Project>('/admin/api/projects', payload),
  update: (
    id: number,
    payload: { name?: string; description?: string; is_active?: boolean },
  ) => api.patch<Project>(`/admin/api/projects/${id}`, payload),
  remove: (id: number) => api.del(`/admin/api/projects/${id}`),
  shareWithUsers: (id: number, user_ids: number[]) =>
    api.patch<Project>(`/admin/api/projects/${id}/share/users`, { user_ids }),
  shareWithGroups: (id: number, group_ids: number[]) =>
    api.patch<Project>(`/admin/api/projects/${id}/share/groups`, { group_ids }),
  // Фаза A: целевые домены проекта
  listDomains: (id: number, query?: { include_deleted?: boolean }) =>
    api.get<ProjectDomain[]>(`/admin/api/projects/${id}/domains`, query as Record<string, boolean | undefined>),
  addDomain: (id: number, domain: string) =>
    api.post<AddDomainResult>(`/admin/api/projects/${id}/domains`, { domain }),
  addDomains: (id: number, domains: string[]) =>
    api.post<BulkAddDomainsResult>(`/admin/api/projects/${id}/domains/bulk`, { domains }),
  removeDomain: (id: number, domainId: number) =>
    api.del(`/admin/api/projects/${id}/domains/${domainId}`),
  // Two-level delete (super_admin only) для money-домена
  restoreDomain: (id: number, domainId: number) =>
    api.post(`/admin/api/projects/${id}/domains/${domainId}/restore`, {}),
  purgeDomain: (id: number, domainId: number) =>
    api.del(`/admin/api/projects/${id}/domains/${domainId}/purge`),
  domainAnalytics: (id: number) =>
    api.get<DomainAnalyticsRow[]>(`/admin/api/projects/${id}/domain-analytics`),
  domainSummary: (id: number, domain: string) =>
    api.get<DomainSummary>(`/admin/api/projects/${id}/domains/${encodeURIComponent(domain)}/summary`),
  domainItems: (id: number, domain: string, query?: { cursor?: number; limit?: number; status?: string }) =>
    api.get<DomainItemsResponse>(
      `/admin/api/projects/${id}/domains/${encodeURIComponent(domain)}/items`,
      query as Record<string, string | number | undefined>,
    ),
  domainRuns: (id: number, domain: string) =>
    api.get<DomainRunRow[]>(`/admin/api/projects/${id}/domains/${encodeURIComponent(domain)}/runs`),
  domainPlacements: (id: number, domain: string) =>
    api.get<DomainPlacement[]>(`/admin/api/projects/${id}/domains/${encodeURIComponent(domain)}/placements`),
}

// ─── Roles & permissions ─────────────────────────────────────────────

export const roles = {
  list: () => api.get<Role[]>('/admin/api/roles'),
  create: (payload: {
    name: string
    description?: string
    permission_ids?: number[]
    page_ids?: number[]
    is_assignable_by_group_admin?: boolean
  }) => api.post<Role>('/admin/api/roles', payload),
  update: (
    id: number,
    payload: {
      name?: string
      description?: string
      is_active?: boolean
      is_assignable_by_group_admin?: boolean
      permission_ids?: number[]
      page_ids?: number[]
    },
  ) => api.patch<Role>(`/admin/api/roles/${id}`, payload),
  remove: (id: number) => api.del(`/admin/api/roles/${id}`),
}

export const permissions = {
  list: () => api.get<Permission[]>('/admin/api/permissions'),
}

// ─── Pages ───────────────────────────────────────────────────────────

export const pages = {
  list: () => api.get<AdminPage[]>('/admin/api/pages'),
  me: () => api.get<string[]>('/admin/api/pages/me'),
  update: (
    id: number,
    payload: {
      name?: string
      description?: string
      is_active?: boolean
      role_ids?: number[]
      user_ids?: number[]
    },
  ) => api.patch<AdminPage>(`/admin/api/pages/${id}`, payload),
}

// ─── Invitations ─────────────────────────────────────────────────────

export const invitations = {
  list: (include_used = true) =>
    api.get<Invitation[]>('/admin/api/invitations', { include_used }),
  create: (payload: {
    group_id?: number | null
    role_ids?: number[]
    email?: string
    note?: string
    ttl_hours?: number
  }) => api.post<CreatedInvitation>('/admin/api/invitations', payload),
  revoke: (id: number) => api.post(`/admin/api/invitations/${id}/revoke`),
  remove: (id: number) => api.del(`/admin/api/invitations/${id}`),
}

// Public — без авторизации (для /register страницы)
export const publicInvitations = {
  view: (token: string) =>
    api.get<PublicInvitationView>(`/admin/api/public/invitations/${encodeURIComponent(token)}`),
  accept: (
    token: string,
    payload: { username: string; password: string; email?: string; full_name?: string },
  ) =>
    api.post<LoginResponse>(
      `/admin/api/public/invitations/${encodeURIComponent(token)}/accept`,
      payload,
    ),
}

// ─── Supplier access (временные доступы поставщиков) ─────────────────

export interface SupplierAccessCreated {
  user_id: number
  username: string
  expires_at: string
  note: string | null
  handover: 'password' | 'link'
  password: string | null
  magic_url: string | null
  login_url: string
  granted_batches: number
}

export interface SupplierAccessItem {
  user_id: number
  username: string
  note: string | null
  is_active: boolean
  expires_at: string | null
  is_expired: boolean
  created_at: string
  last_login_at: string | null
  handover: 'password' | 'link'
  password: string | null   // расшифрованный пароль (super_admin-only эндпоинт)
}

export const supplierAccess = {
  list: () => api.get<{ items: SupplierAccessItem[] }>('/admin/api/supplier-access'),
  create: (payload: {
    note?: string
    ttl_hours?: number
    handover?: 'password' | 'link'
    batch_ids?: number[]
  }) => api.post<SupplierAccessCreated>('/admin/api/supplier-access', payload),
  revoke: (userId: number) => api.post(`/admin/api/supplier-access/${userId}/revoke`),
}

// Public — magic-login поставщика (страница /portal-login → редирект на /batches)
export const publicPortal = {
  login: (token: string) =>
    api.post<LoginResponse>(`/admin/api/public/portal/login?token=${encodeURIComponent(token)}`),
}

// ─── WP Sites + Credentials ──────────────────────────────────────────

export const wpSites = {
  list: (query?: {
    cursor?: string
    limit?: number
    search?: string
    status?:
      | 'all' | 'active' | 'auto-disabled' | 'off'
      | 'usable' | 'unusable' | 'cred_valid' | 'cred_invalid' | 'cred_transient'
      | 'rpc_postable' | 'admin_capable' | 'admin_postable'
    sort?: 'alpha' | 'recent' | 'valid_desc' | 'transient_desc' | 'most_used'
  }) =>
    api.get<WpSiteList>(
      '/admin/api/wp-sites',
      query as Record<string, string | number | boolean | undefined>,
    ),
  summary: (opts?: { live?: boolean }) =>
    api.get<WpPoolSummary>('/admin/api/wp-sites/summary', opts?.live ? { live: true } : undefined),
  credentialTags: () => api.get<string[]>('/admin/api/wp-sites/credential-tags'),
  credentialTagsStats: () =>
    api.get<{ tag: string; sites: number }[]>('/admin/api/wp-sites/credential-tags-stats'),
  get: (id: number, opts?: { include_password?: boolean }) =>
    api.get<WpSiteDetail>(`/admin/api/wp-sites/${id}`, opts as Record<string, boolean | undefined>),
  analytics: (id: number) => api.get<SiteAnalytics>(`/admin/api/wp-sites/${id}/analytics`),
  events: (id: number, limit = 50) =>
    api.get<SiteEvent[]>(`/admin/api/wp-sites/${id}/events`, { limit }),
  create: (payload: {
    domain: string
    hint_path?: string | null
    hint_port?: number | null
    note?: string
  }) => api.post<WpSiteDetail>('/admin/api/wp-sites', payload),
  update: (
    id: number,
    payload: {
      domain?: string
      hint_path?: string | null
      hint_port?: number | null
      is_active?: boolean
      note?: string | null
    },
  ) => api.patch<WpSiteDetail>(`/admin/api/wp-sites/${id}`, payload),
  remove: (id: number) => api.del(`/admin/api/wp-sites/${id}`),
  importCsv: (file: File, tag?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    if (tag) fd.append('tag', tag)
    return api.upload<WpImportResult>('/admin/api/wp-sites/import', fd)
  },
  triggerValidate: (scope: 'all' | 'invalid' | 'transient' | 'stale' = 'all') =>
    api.post<{ ok: boolean; running: boolean; scope?: string; message?: string }>(
      `/admin/api/wp-sites/validate?scope=${scope}`,
    ),
  validationStatus: () =>
    api.get<WpValidationState>('/admin/api/wp-sites/validation-status'),
  // ─── Bulk by filter ───
  bulkFilterCount: (f: { status?: string; tag?: string; source?: string; search?: string }) => {
    const q = new URLSearchParams()
    if (f.status) q.set('status', f.status)
    if (f.tag) q.set('tag', f.tag)
    if (f.source) q.set('source', f.source)
    if (f.search) q.set('search', f.search)
    return api.get<{ count: number }>(`/admin/api/wp-sites/credentials/bulk-filter-count?${q}`)
  },
  bulkDeleteByFilter: (f: { status?: string; tag?: string; source?: string; search?: string }) => {
    const q = new URLSearchParams()
    if (f.status) q.set('status', f.status)
    if (f.tag) q.set('tag', f.tag)
    if (f.source) q.set('source', f.source)
    if (f.search) q.set('search', f.search)
    return api.post<{ deleted: number }>(`/admin/api/wp-sites/credentials/bulk-delete-by-filter?${q}`)
  },
  // ─── Provision (создание наших аккаунтов) ───
  provisionSite: (siteId: number, role: 'author' | 'editor' | 'administrator' = 'author') =>
    api.post<{ ok: boolean; status: string; domain?: string; login?: string; role?: string; via?: string; error?: string }>(
      `/admin/api/wp-sites/${siteId}/provision`, { role },
    ),
  provisionCount: () =>
    api.get<{ provisionable: number }>('/admin/api/wp-sites/credentials/provision-count'),
  bulkProvision: (role: 'author' | 'editor' | 'administrator' = 'author', concurrency = 4) =>
    api.post<{ ok: boolean; role: string; provisionable: number }>(
      '/admin/api/wp-sites/credentials/bulk-provision', { role, concurrency },
    ),
}

export const wpCredentials = {
  listForSite: (siteId: number, opts?: { include_password?: boolean }) =>
    api.get<WpCredential[]>(`/admin/api/wp-sites/${siteId}/credentials`, opts),
  create: (siteId: number, payload: { login: string; password: string; tags?: string[]; note?: string }) =>
    api.post<WpCredential>(`/admin/api/wp-sites/${siteId}/credentials`, { site_id: siteId, ...payload }),
  update: (
    credId: number,
    payload: {
      login?: string
      password?: string
      tags?: string[] | null
      note?: string | null
      is_valid?: boolean
    },
  ) => api.patch<WpCredential>(`/admin/api/wp-sites/credentials/${credId}`, payload),
  remove: (credId: number) => api.del(`/admin/api/wp-sites/credentials/${credId}`),
  bulkRemove: (ids: number[]) =>
    api.post<{ deleted: number }>('/admin/api/wp-sites/credentials/bulk-delete', { ids }),
}

// ─── WP Import Batches ───────────────────────────────────────────────

export const wpBatches = {
  list: (query?: { limit?: number }) =>
    api.get<{ items: WpBatch[] }>('/admin/api/batches', query as Record<string, string | number | undefined>),
  get: (id: number) => api.get<WpBatch>(`/admin/api/batches/${id}`),
  create: (
    file: File,
    fields: {
      name: string
      tag?: string
      note?: string
      cost_total?: number
      cost_currency?: string
      auto_validate?: boolean
      auto_provision?: boolean
    },
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('name', fields.name)
    if (fields.tag) fd.append('tag', fields.tag)
    if (fields.note) fd.append('note', fields.note)
    if (fields.cost_total !== undefined && fields.cost_total !== null)
      fd.append('cost_total', String(fields.cost_total))
    if (fields.cost_currency) fd.append('cost_currency', fields.cost_currency)
    if (fields.auto_validate !== undefined) fd.append('auto_validate', String(fields.auto_validate))
    if (fields.auto_provision !== undefined) fd.append('auto_provision', String(fields.auto_provision))
    return api.upload<WpBatchImportResult>('/admin/api/batches', fd)
  },
  validate: (
    id: number,
    payload: {
      scope?: 'all' | 'invalid' | 'pending'
      concurrency?: number
      proxy_id?: number | null
      detect_language?: boolean
      level?: 'light' | 'medium' | 'full'
      provision_after?: boolean
      provision_role?: 'author' | 'editor' | 'administrator'
    },
  ) => api.post<{ ok: boolean }>(`/admin/api/batches/${id}/validate`, payload),
  pause: (id: number) => api.post<void>(`/admin/api/batches/${id}/pause`),
  resume: (id: number) => api.post<{ ok: boolean }>(`/admin/api/batches/${id}/resume`),
  revalidateFailed: (id: number) =>
    api.post<{ ok: boolean }>(`/admin/api/batches/${id}/revalidate-failed`),
  resetValidation: (id: number, confirm_name: string) =>
    api.post<{ ok: boolean; creds_reset: number }>(
      `/admin/api/batches/${id}/reset-validation`,
      { confirm_name },
    ),
  remove: (id: number) => api.del(`/admin/api/batches/${id}`),
  credentials: (
    id: number,
    query?: {
      status?: 'valid' | 'invalid' | 'transient' | 'pending' | 'duplicates'
      search?: string
      after_id?: number
      limit?: number
      include_password?: boolean
    },
  ) =>
    api.get<BatchCredListResponse>(
      `/admin/api/batches/${id}/credentials`,
      query as Record<string, string | number | undefined>,
    ),
  forceCredStatus: (id: number, credId: number, is_valid: boolean) =>
    api.post<{ ok: boolean; is_valid: boolean }>(
      `/admin/api/batches/${id}/credentials/${credId}/force-status`,
      { is_valid },
    ),
  provisionCount: (id: number) =>
    api.get<{ batch_id: number; provisionable: number }>(`/admin/api/batches/${id}/provision-count`),
  provision: (id: number, role: 'author' | 'editor' | 'administrator' = 'author', concurrency = 4) =>
    api.post<{ ok: boolean; batch_id: number; role: string; provisionable: number }>(
      `/admin/api/batches/${id}/provision`, { role, concurrency },
    ),
}

// ─── Audit log ───────────────────────────────────────────────────────

export const audit = {
  list: (query?: {
    actor_id?: number
    action?: string
    action_prefix?: string
    resource_type?: string
    resource_id?: number
    after_id?: number
    limit?: number
  }) =>
    api.get<AuditListResponse>('/admin/api/audit-log', query as Record<string, string | number | undefined>),
  actions: () => api.get<string[]>('/admin/api/audit-log/actions'),
}

// ─── Proxies ─────────────────────────────────────────────────────────

export const proxies = {
  list: (query?: { cursor?: string; search?: string; provider?: string; status?: string; limit?: number }) =>
    api.get<ProxyListResponse>('/admin/api/proxies', query as Record<string, string | number | undefined>),
  providers: () => api.get<ProxyProviderStat[]>('/admin/api/proxies/providers'),
  sources: () => api.get<ProxySourceMeta[]>('/admin/api/proxies/sources'),
  pools: () => api.get<{ all_active: number; providers: Record<string, number> }>('/admin/api/proxies/pools'),
  create: (payload: {
    protocol?: string
    host: string
    port: number
    username?: string
    password?: string
    country?: string
    proxy_type?: string
    provider?: string
    note?: string
  }) => api.post<Proxy>('/admin/api/proxies', payload),
  remove: (id: number) => api.del(`/admin/api/proxies/${id}`),
  check: (id: number) => api.post<ProxyCheckResult>(`/admin/api/proxies/${id}/check`),
  recheckAll: (onlyActive = false) =>
    api.post<{ ok: boolean; total: number; ok_count?: number; down: number }>(
      `/admin/api/proxies/recheck-all${onlyActive ? '?only_active=true' : ''}`,
    ),
  bulk: (text: string) => api.post<ProxyBulkResult>('/admin/api/proxies/bulk', { text }),
  importFromSource: (source: string, opts: Record<string, unknown>) =>
    api.post<ProxyImportResult>(`/admin/api/proxies/import/${source}`, { opts }),
  removeSource: (source: string) => api.del<{ deleted: number }>(`/admin/api/proxies/source/${source}`),
}

// ─── Dashboard ───────────────────────────────────────────────────────

export interface SystemHealth {
  redis_ok: boolean
  celery_queue_depth: number | null
  cf_browser_ok: boolean
  db_pool: { size?: number; checked_out?: number; overflow?: number; checked_in?: number }
  proxies: { total: number; active: number; locked: number; down: number }
  runs_active: number
  batches_validating: number
  recent_failures: Array<{
    text_item_id: number
    domain: string | null
    run_id: number
    error: string
    at: string | null
  }>
}

export const dashboard = {
  get: () => api.get<DashboardData>('/admin/api/dashboard'),
  systemHealth: () => api.get<SystemHealth>('/admin/api/dashboard/system-health'),
}

// ─── App settings ────────────────────────────────────────────────────

export const appSettings = {
  get: () => api.get<AppSettings>('/admin/api/app-settings'),
  update: (payload: {
    default_concurrency?: number
    default_timeout_seconds?: number
    global_posting_concurrency?: number
    cf_browser_concurrency?: number
    posting_concurrency_floor?: number
    site_disable_threshold?: number
    site_disable_threshold_cf?: number
    max_concurrent_batch_validations?: number
    max_concurrent_link_checks?: number
    batch_validation_concurrency?: number
    content_gen_concurrency?: number
    default_publish_from?: string | null
    default_publish_to?: string | null
  }) => api.put<AppSettings>('/admin/api/app-settings', payload),
}

// ─── Global Queue (унифицированный снапшот всех полос работы) ─────────

export const globalQueue = {
  get: () => api.get<GlobalQueueSnapshot>('/admin/api/queue'),
}

// ─── Библиотека текстов (B2: поиск) ──────────────────────────────────

export const texts = {
  search: (query?: { q?: string; lang?: string; reusable_only?: boolean; limit?: number }) => {
    const url = new URL('/admin/api/texts', window.location.origin)
    if (query?.q) url.searchParams.set('q', query.q)
    if (query?.lang) url.searchParams.set('lang', query.lang)
    if (query?.reusable_only) url.searchParams.set('reusable_only', 'true')
    if (query?.limit) url.searchParams.set('limit', String(query.limit))
    return api.get<TextSearchRow[]>(url.pathname + url.search)
  },
  // URL для скачивания CSV (отдаём как ссылку, браузер качает с авторизацией-cookie)
  exportUrl: (query?: { q?: string; lang?: string; reusable_only?: boolean; with_body?: boolean }) => {
    const url = new URL('/admin/api/texts/export.csv', window.location.origin)
    if (query?.q) url.searchParams.set('q', query.q)
    if (query?.lang) url.searchParams.set('lang', query.lang)
    if (query?.reusable_only) url.searchParams.set('reusable_only', 'true')
    if (query?.with_body) url.searchParams.set('with_body', 'true')
    return url.pathname + url.search
  },
  get: (id: number) => api.get<TextDetail>(`/admin/api/texts/${id}`),
  update: (id: number, payload: { title: string | null; body: string }) =>
    api.put<TextDetail>(`/admin/api/texts/${id}`, payload),
}

// ─── Postings (runs) ─────────────────────────────────────────────────

export const postings = {
  listForProject: (projectId: number) =>
    api.get<PostingRun[]>(`/admin/api/projects/${projectId}/postings`),
  list: (query?: {
    cursor?: string
    limit?: number
    statuses?: string[]
    project_id?: number
    created_by?: number
    search?: string
    include_deleted?: boolean
  }) => {
    // statuses передаём как multi-query — собираем URL вручную
    const url = new URL('/admin/api/postings', window.location.origin)
    if (query?.cursor) url.searchParams.set('cursor', query.cursor)
    if (query?.limit) url.searchParams.set('limit', String(query.limit))
    if (query?.project_id) url.searchParams.set('project_id', String(query.project_id))
    if (query?.created_by) url.searchParams.set('created_by', String(query.created_by))
    if (query?.search) url.searchParams.set('search', query.search)
    if (query?.include_deleted) url.searchParams.set('include_deleted', 'true')
    if (query?.statuses) for (const s of query.statuses) url.searchParams.append('statuses', s)
    return api.get<Paginated<PostingRun>>(url.pathname + url.search)
  },
  // Глобальная очередь — все active runs по всем юзерам
  queue: () => api.get<QueueResponse>('/admin/api/postings/queue'),
  get: (runId: number) => api.get<PostingRun>(`/admin/api/postings/${runId}`),
  create: (projectId: number, file: File, params: CreateRunParams) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('params', JSON.stringify(params))
    return api.upload<PostingRun>(`/admin/api/projects/${projectId}/postings`, fd)
  },
  // csv-direct: csv/xlsx со столбцами link, anchor, text
  createCsvDirect: (projectId: number, file: File, params: CreateRunParams) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('params', JSON.stringify(params))
    return api.upload<PostingRun>(`/admin/api/projects/${projectId}/postings/csv-direct`, fd)
  },
  // кампания: csv/xlsx (links, anchor, counts) → reuse из библиотеки
  createCampaign: (projectId: number, file: File, params: CreateRunParams) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('params', JSON.stringify(params))
    return api.upload<PostingRun>(`/admin/api/projects/${projectId}/postings/campaign`, fd)
  },
  progress: (runId: number) =>
    api.get<RunProgress>(`/admin/api/postings/${runId}/progress`),
  resolveBulk: (runId: number, domain: string) =>
    api.post<{ resolved: number; skipped: number; total: number }>(
      `/admin/api/postings/${runId}/resolve-bulk`,
      { domain },
    ),
  needsReviewDomains: (runId: number) =>
    api.get<{ domain: string; count: number; is_project_domain: boolean }[]>(
      `/admin/api/postings/${runId}/needs-review-domains`,
    ),
  addProjectDomain: (runId: number, domain: string) =>
    api.post<{ domain: string; created: boolean; auto_resolved_runs: number }>(
      `/admin/api/postings/${runId}/add-project-domain`,
      { domain },
    ),
  // Целевые домены прогона (явные ссылки CSV), которых ещё нет в проекте.
  missingProjectDomains: (runId: number) =>
    api.get<{ domain: string; count: number }[]>(
      `/admin/api/postings/${runId}/missing-project-domains`,
    ),
  addProjectDomains: (runId: number, domains: string[]) =>
    api.post<{ added: string[]; duplicates: string[]; invalid: string[]; auto_resolved_runs: number }>(
      `/admin/api/postings/${runId}/add-project-domains`,
      { domains },
    ),
  textItems: (
    runId: number,
    query?: { cursor?: string; limit?: number; status?: string },
  ) =>
    api.get<Paginated<TextItem>>(
      `/admin/api/postings/${runId}/text-items`,
      query as Record<string, string | number | undefined>,
    ),
  deleteTextItem: (runId: number, itemId: number) =>
    api.post<{ ok: boolean; deleted_status: string; run_status: string | null }>(
      `/admin/api/postings/${runId}/text-items/${itemId}/delete`,
      {},
    ),
  update: (
    runId: number,
    payload: {
      max_posts_per_site?: number
      priority?: 'low' | 'normal' | 'high'
      scheduled_for?: string | null
      spread_days?: number
      posting_method?: 'auto' | 'xmlrpc_only' | 'admin_only'
      post_verify?: 'mark' | 'auto'
      proxy_selector?: string | null
      pool_fallback?: boolean
      publish_from?: string | null
      publish_to?: string | null
      site_langs?: string | null
      site_tlds?: string | null
      site_tags?: string | null
      site_domains?: string | null
      site_domains_key?: string | null
    },
  ) => api.patch<PostingRun>(`/admin/api/postings/${runId}`, payload),
  start: (runId: number) =>
    api.post<{ ok: boolean; run_id: number; status: string }>(`/admin/api/postings/${runId}/start`),
  validateLinks: (runId: number) =>
    api.post<{ ok: boolean; run_id: number; total: number }>(
      `/admin/api/postings/${runId}/validate-links`,
    ),
  restart: (runId: number) =>
    api.post<{ ok: boolean; run_id: number; status: string; items_reset: number }>(
      `/admin/api/postings/${runId}/restart`,
    ),
  pause: (runId: number) => api.post<void>(`/admin/api/postings/${runId}/pause`),
  resume: (runId: number) => api.post<void>(`/admin/api/postings/${runId}/resume`),
  cancel: (runId: number) => api.post<void>(`/admin/api/postings/${runId}/cancel`),
  retryFailed: (runId: number) =>
    api.post<{ retried: number; re_enqueued: boolean }>(
      `/admin/api/postings/${runId}/retry-failed`,
    ),
  remove: (runId: number) => api.del(`/admin/api/postings/${runId}`),
  // Two-level delete (super_admin only)
  restore: (runId: number) => api.post(`/admin/api/postings/${runId}/restore`, {}),
  purge: (runId: number) => api.del(`/admin/api/postings/${runId}/purge`),
  // ─── Link runs (sitewide / homepage) ───
  linkCandidates: (projectId: number) =>
    api.get<{ candidates: number }>(`/admin/api/projects/${projectId}/postings/link-candidates`),
  // link-run из файла anchor,link,count (count = на сколько сайтов ставить ссылку)
  createLinkRun: (
    projectId: number,
    file: File,
    params: {
      name: string
      task_type: 'sitewide_link' | 'homepage_link'
      priority?: 'low' | 'normal' | 'high'
      site_langs?: string | null
      site_tlds?: string | null
      max_posts_per_site?: number
      scheduled_for?: string | null
      spread_days?: number
      proxy_selector?: string | null
      publish_from?: string | null
      publish_to?: string | null
      site_tags?: string | null
      site_domains?: string | null
      site_domains_key?: string | null
      hide_methods?: string[]
    },
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('params', JSON.stringify(params))
    return api.upload<PostingRun>(`/admin/api/projects/${projectId}/postings/links`, fd)
  },
  uploadDomainList: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.upload<{ key: string; count: number }>('/admin/api/postings/domain-list', fd)
  },
  removeLink: (runId: number, itemId: number) =>
    api.post<{ ok: boolean; status: string }>(
      `/admin/api/postings/${runId}/text-items/${itemId}/remove-link`,
    ),
  // Пер-айтем действия (кнопки в таблице) — все async (202), UI поллит статус
  generateItem: (runId: number, itemId: number) =>
    api.post<{ ok: boolean; item_id: number; status: string }>(
      `/admin/api/postings/${runId}/text-items/${itemId}/generate`),
  regenerateItem: (runId: number, itemId: number) =>
    api.post<{ ok: boolean; item_id: number; status: string }>(
      `/admin/api/postings/${runId}/text-items/${itemId}/regenerate`),
  postItem: (runId: number, itemId: number) =>
    api.post<{ ok: boolean; item_id: number; status: string }>(
      `/admin/api/postings/${runId}/text-items/${itemId}/post`),
  repostItem: (runId: number, itemId: number) =>
    api.post<{ ok: boolean; item_id: number; status: string }>(
      `/admin/api/postings/${runId}/text-items/${itemId}/repost`),
  // Bulk «Сгенерировать тексты» (manual): фоном генерит все тексты задачи
  generateTexts: (runId: number) =>
    api.post<{ ok: boolean; run_id: number; status: string }>(
      `/admin/api/postings/${runId}/generate-texts`),
  // Bulk «Заполнить спины» (manual gen_per_row): расшить оригиналы в спины без старта
  fillSpins: (runId: number) =>
    api.post<{ ok: boolean; run_id: number; status: string }>(
      `/admin/api/postings/${runId}/fill-spins`),
  // Content Engine: spin-fanout (C1)
  createSpinRun: (projectId: number, params: CreateSpinRunParams) =>
    api.post<PostingRun>(`/admin/api/projects/${projectId}/postings/spin`, params),
  spinOriginals: (runId: number) =>
    api.get<SpinOriginalRow[]>(`/admin/api/postings/${runId}/originals`),
  updateSpinOriginal: (runId: number, textId: number, body: { body: string; title?: string | null }) =>
    api.put<{ ok: boolean }>(`/admin/api/postings/${runId}/originals/${textId}`, body),
}

// ─── Text items (edit) ──────────────────────────────────────────────

export const textItems = {
  get: (id: number) => api.get<TextItemDetail>(`/admin/api/text-items/${id}`),
  update: (id: number, payload: { title: string | null; content: string }) =>
    api.put<TextItemDetail>(`/admin/api/text-items/${id}`, payload),
  updateRemote: (id: number) =>
    api.post<{ ok: boolean; status: string; via?: string; domain?: string }>(
      `/admin/api/text-items/${id}/update-remote`,
    ),
  deleteRemote: (id: number) =>
    api.post<{ ok: boolean; status: string; via?: string; domain?: string }>(
      `/admin/api/text-items/${id}/delete-remote`,
    ),
  // Фаза A: дозаполнить needs_review-задачу (целевая ссылка + анкор)
  resolve: (id: number, payload: { link: string; anchor: string }) =>
    api.post<{ item_id: number; target_domain: string; status: string }>(
      `/admin/api/text-items/${id}/resolve`, payload,
    ),
}

// ─── AI Settings (C2): провайдеры / модели / шаблоны промптов ───
export const aiSettings = {
  listProviders: () => api.get<AiProvider[]>('/admin/api/ai/providers'),
  createProvider: (payload: {
    name: string; type: string; api_key: string; base_url?: string | null; is_active?: boolean
  }) => api.post<AiProvider>('/admin/api/ai/providers', payload),
  updateProvider: (
    id: number,
    payload: { name?: string; type?: string; api_key?: string; base_url?: string | null; is_active?: boolean },
  ) => api.patch<AiProvider>(`/admin/api/ai/providers/${id}`, payload),
  deleteProvider: (id: number) => api.del(`/admin/api/ai/providers/${id}`),
  shareProvider: (id: number, payload: AiShareRequest) =>
    api.post<AiProvider>(`/admin/api/ai/providers/${id}/share`, payload),

  createModel: (payload: {
    provider_id: number; display_name: string; model_id: string
    temperature?: number; max_tokens?: number; purpose?: string; is_active?: boolean
  }) => api.post<AiModel>('/admin/api/ai/models', payload),
  updateModel: (
    id: number,
    payload: {
      display_name?: string; model_id?: string; temperature?: number
      max_tokens?: number; purpose?: string; is_active?: boolean
    },
  ) => api.patch<AiModel>(`/admin/api/ai/models/${id}`, payload),
  deleteModel: (id: number) => api.del(`/admin/api/ai/models/${id}`),

  listPrompts: () => api.get<PromptTemplate[]>('/admin/api/ai/prompts'),
  createPrompt: (payload: { name: string; body: string; notes?: string | null }) =>
    api.post<PromptTemplate>('/admin/api/ai/prompts', payload),
  updatePrompt: (id: number, payload: { name?: string; body?: string; notes?: string | null }) =>
    api.patch<PromptTemplate>(`/admin/api/ai/prompts/${id}`, payload),
  deletePrompt: (id: number) => api.del(`/admin/api/ai/prompts/${id}`),
  sharePrompt: (id: number, payload: AiShareRequest) =>
    api.post<PromptTemplate>(`/admin/api/ai/prompts/${id}/share`, payload),
}
