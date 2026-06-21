"""Операции над уже опубликованным постом: update / delete на живом сайте.

По сути тот же постинг: логинимся доступами к домену и шлём edit/delete.
Если у домена несколько валидных кредов — перебираем их (XML-RPC → admin),
пока какой-нибудь не сработает (на случай если один доступ отвалился)."""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from core.config import settings
from core.crypto import decrypt_password
from core.db import WriteSession
from core.storage import StorageError, storage
from infrastructure.db.models.posting import TextItem, TextItemStatus
from infrastructure.db.models.wp_access import WpCredential
from infrastructure.wp_admin_client import AdminLoginKind, WpAdminClient
from infrastructure.wp_client import XmlRpcPoster

log = structlog.get_logger(__name__)
OP_TIMEOUT_S = 30


def _read_content(storage_key: str | None) -> str:
    if not storage_key:
        return ""
    try:
        raw = storage.get_bytes(settings.MINIO_BUCKET_TEXT_ITEMS, storage_key)
    except StorageError:
        return ""
    return raw.decode("utf-8", errors="replace")


async def _candidate_creds(session, site_id: int):
    """Валидные креды сайта; XML-RPC-способные первыми (они дешевле)."""
    rows = (await session.execute(
        select(WpCredential).where(
            WpCredential.site_id == site_id,
            WpCredential.deleted_at.is_(None),
            WpCredential.cred_status == "valid",
        ).order_by(WpCredential.can_xmlrpc.is_(True).desc(), WpCredential.id.asc())
    )).scalars().all()
    return [(c.id, c.login, decrypt_password(c.password),
             c.can_xmlrpc, c.can_admin_login) for c in rows]


async def _run_op(item_id: int, *, op: str, actor_id: int | None) -> dict:
    """op = 'update' | 'delete'. Перебор кредов до первого успеха."""
    from domain.proxies.service import pick_active_proxy_url
    from domain.wp_batches.service import _build_http_client_url

    async with WriteSession() as s:
        item = await s.scalar(
            select(TextItem).options(selectinload(TextItem.site))
            .where(TextItem.id == item_id))
        if item is None or not item.site:
            return {"ok": False, "status": "not_found"}
        if not item.post_id:
            return {"ok": False, "status": "no_post_id"}
        site = item.site
        post_id = item.post_id
        title = item.title or ""
        if op == "update":
            from domain.texts import read_item_body
            content = await read_item_body(s, text_id=item.text_id, storage_key=item.storage_key)
        else:
            content = ""
        domain = site.domain
        creds = await _candidate_creds(s, site.id)
        _purl = await pick_active_proxy_url(s)  # residential exit для curl_cffi-fallback

    if not creds:
        return {"ok": False, "status": "no_valid_creds", "domain": domain}

    tried: list[str] = []
    http = await _build_http_client_url(_purl)
    try:
        async with http:
            poster = XmlRpcPoster(http, timeout_seconds=OP_TIMEOUT_S, proxy_url=_purl)
            admin = WpAdminClient(http, timeout_seconds=OP_TIMEOUT_S, proxy_url=_purl)
            for cid, login, pw, can_rpc, can_adm in creds:
                # 1) XML-RPC
                if can_rpc is not False:
                    if op == "update":
                        out = await poster.edit_post(site, login, pw, post_id, title, content)
                    else:
                        out = await poster.delete_post(site, login, pw, post_id)
                    if out.success:
                        return await _finalize_ok(item_id, op, cid, "xmlrpc", domain)
                    tried.append(f"{login}/rpc:{out.error.value}")
                # 2) admin REST
                if can_adm is not False:
                    lo = await admin.login(site=site, login=login, password=pw)
                    if lo.error == AdminLoginKind.OK:
                        if op == "update":
                            out = await admin.update_post_via_rest(site, post_id, title, content)
                        else:
                            out = await admin.delete_post_via_rest(site, post_id)
                        if out.success:
                            return await _finalize_ok(item_id, op, cid, "admin", domain)
                        tried.append(f"{login}/admin:{out.error.value}")
                    else:
                        tried.append(f"{login}/login:{lo.error.value}")
    except Exception as e:
        log.warning("post_ops.exception", item_id=item_id, op=op, error=str(e))
        return {"ok": False, "status": "error", "domain": domain, "error": str(e)[:200]}

    return {"ok": False, "status": "all_creds_failed", "domain": domain, "tried": tried}


async def _finalize_ok(item_id: int, op: str, cred_id: int, via: str, domain: str) -> dict:
    now = datetime.now(UTC)
    async with WriteSession() as s:
        if op == "update":
            await s.execute(update(TextItem).where(TextItem.id == item_id).values(
                credential_id=cred_id, posted_at=now, last_error=None))
        else:  # delete — пост снят с сайта
            await s.execute(update(TextItem).where(TextItem.id == item_id).values(
                status=TextItemStatus.SKIPPED.value, posted_url=None, post_id=None,
                last_error=f"удалён с сайта ({via})"))
        await s.commit()
    log.info("post_ops.done", item_id=item_id, op=op, via=via, domain=domain)
    return {"ok": True, "status": "updated" if op == "update" else "deleted",
            "via": via, "domain": domain}


async def update_remote_post(item_id: int, *, actor_id: int | None = None) -> dict:
    return await _run_op(item_id, op="update", actor_id=actor_id)


async def delete_remote_post(item_id: int, *, actor_id: int | None = None) -> dict:
    return await _run_op(item_id, op="delete", actor_id=actor_id)
