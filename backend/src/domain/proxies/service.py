"""
Proxies service: CRUD + bulk parse + import-from-source + health check.

Password encryption — через core/crypto (как у WP credentials).
Health-check — httpx GET https://api.ipify.org через proxy + ip-api.com lookup.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import UTC, datetime

import httpx
import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.crypto import decrypt_password, encrypt_password
from infrastructure.db.models import Proxy
from infrastructure.proxy_sources import ImportedProxy, get_source

log = structlog.get_logger(__name__)


# ─── CRUD ────────────────────────────────────────────────────────────


async def list_proxies(
    session: AsyncSession,
    *,
    search: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    after_id: int | None = None,
    limit: int = 200,
) -> list[Proxy]:
    stmt = select(Proxy).order_by(Proxy.id.desc()).limit(limit + 1)
    if after_id:
        stmt = stmt.where(Proxy.id < after_id)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Proxy.host.ilike(like),
                Proxy.external_ip.ilike(like),
                Proxy.isp.ilike(like),
                Proxy.country.ilike(like),
                Proxy.note.ilike(like),
            )
        )
    if provider:
        stmt = stmt.where(Proxy.provider == provider)
    if status:
        stmt = stmt.where(Proxy.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def get_proxy(session: AsyncSession, proxy_id: int) -> Proxy | None:
    return await session.scalar(select(Proxy).where(Proxy.id == proxy_id))


async def create_manual(
    session: AsyncSession,
    *,
    protocol: str,
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    country: str | None = None,
    proxy_type: str | None = None,
    provider: str | None = None,
    note: str | None = None,
) -> Proxy:
    p = Proxy(
        protocol=protocol,
        host=host.strip(),
        port=port,
        username=username.strip() if username else None,
        password=encrypt_password(password) if password else None,
        country=(country or "").strip().upper() or None,
        proxy_type=proxy_type,
        provider=provider,
        note=note,
        source="manual",
    )
    session.add(p)
    await session.commit()
    refreshed = await get_proxy(session, p.id)
    assert refreshed is not None
    return refreshed


async def delete_proxy(session: AsyncSession, proxy_id: int) -> None:
    await session.execute(Proxy.__table__.delete().where(Proxy.id == proxy_id))
    await session.commit()


async def delete_by_source(session: AsyncSession, source: str) -> int:
    res = await session.execute(Proxy.__table__.delete().where(Proxy.source == source))
    await session.commit()
    return int(res.rowcount or 0)


async def provider_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Proxy.source, func.count(Proxy.id)).group_by(Proxy.source)
        )
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


async def pool_stats(session: AsyncSession) -> dict:
    """
    Breakdown активных прокси для UI dropdown в posting run.
      {
        "all_active": 2550,
        "providers": {"webshare": 2550, "decodo": 0, ...},
      }

    Считаем по `is_active=True` ВНЕ зависимости от `status`. Большинство
    прокси из bulk-импорта имеют `status='unknown'` (не прогонялись через
    check) — они не «мёртвые», просто непроверенные. Worker всё равно
    попробует их использовать; если прокси не отвечает — отлетит как
    NETWORK и worker возьмёт следующую из пула.
    """
    rows = (
        await session.execute(
            select(Proxy.source, func.count(Proxy.id))
            .where(Proxy.is_active.is_(True))
            .group_by(Proxy.source)
        )
    ).all()
    providers = {str(r[0] or "unknown"): int(r[1]) for r in rows}
    return {
        "all_active": sum(providers.values()),
        "providers": providers,
    }


# ─── Bulk parse: парсим текстовку построчно ──────────────────────────


_BULK_LINE_RE = re.compile(
    r"""^\s*
        (?:(?P<scheme>https?|socks5?)://)?
        (?:(?P<user>[^:@\s]+)(?::(?P<pass>[^@\s]+))?@)?
        (?P<host>[^:\s@/]+)
        :(?P<port>\d{1,5})
        \s*$
    """,
    re.VERBOSE,
)


def parse_bulk(text: str) -> tuple[list[dict], list[str]]:
    """
    Поддерживает несколько форматов на строку:

      host:port
      host:port:user:pass
      user:pass@host:port
      http://user:pass@host:port
      socks5://host:port

    Возвращает (parsed_rows, invalid_lines).
    """
    parsed: list[dict] = []
    invalid: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _BULK_LINE_RE.match(line)
        if m:
            parsed.append({
                "protocol": (m.group("scheme") or "http").lower(),
                "host": m.group("host"),
                "port": int(m.group("port")),
                "username": m.group("user"),
                "password": m.group("pass"),
            })
            continue
        # host:port:user:pass — Webshare-style
        parts = line.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            parsed.append({
                "protocol": "http",
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] or None,
                "password": parts[3] or None,
            })
            continue
        invalid.append(line)
    return parsed, invalid


async def bulk_create(session: AsyncSession, rows: list[dict]) -> int:
    inserted = 0
    for r in rows:
        host = (r.get("host") or "").strip()
        port = r.get("port")
        if not host or not isinstance(port, int):
            continue
        p = Proxy(
            protocol=r.get("protocol") or "http",
            host=host,
            port=port,
            username=r.get("username"),
            password=encrypt_password(r["password"]) if r.get("password") else None,
            source="bulk",
        )
        session.add(p)
        inserted += 1
    if inserted:
        await session.commit()
    return inserted


# ─── Import from source ─────────────────────────────────────────────


async def import_from_source(
    session: AsyncSession, source_name: str, opts: dict
) -> dict:
    """
    Дёргает source.fetch(**opts) → upsert по (source, source_id).
    Старые записи этого source НЕ удаляются — для очистки есть delete_by_source.
    Возвращает {created, updated, total_in_db}.
    """
    fn = get_source(source_name)
    if fn is None:
        raise ValueError(f"Unknown source: {source_name}")

    rows: list[ImportedProxy] = await fn(**opts)

    created = 0
    updated = 0
    for r in rows:
        existing = await session.scalar(
            select(Proxy).where(Proxy.source == source_name, Proxy.source_id == r.source_id)
        )
        if existing:
            existing.host = r.host
            existing.port = r.port
            existing.protocol = r.protocol
            if r.username is not None:
                existing.username = r.username
            if r.password is not None:
                existing.password = encrypt_password(r.password)
            if r.country is not None:
                existing.country = r.country.upper()
            if r.provider is not None:
                existing.provider = r.provider
            if r.proxy_type is not None and not existing.proxy_type:
                existing.proxy_type = r.proxy_type
            updated += 1
        else:
            session.add(Proxy(
                protocol=r.protocol,
                host=r.host,
                port=r.port,
                username=r.username,
                password=encrypt_password(r.password) if r.password else None,
                country=r.country.upper() if r.country else None,
                provider=r.provider,
                proxy_type=r.proxy_type,
                source=source_name,
                source_id=r.source_id,
            ))
            created += 1

    if created or updated:
        await session.commit()

    total = int(
        (await session.execute(
            select(func.count(Proxy.id)).where(Proxy.source == source_name)
        )).scalar_one()
    )
    log.info("proxies.import.done", source=source_name, created=created, updated=updated, total=total)
    return {"created": created, "updated": updated, "total_in_db": total}


# ─── Health check (single + bulk) ────────────────────────────────────


def proxy_url(p: Proxy) -> str:
    """`http://user:pass@host:port` с расшифрованным паролем."""
    creds = ""
    if p.username:
        pw = decrypt_password(p.password) if p.password else ""
        creds = f"{p.username}:{pw}@" if pw else f"{p.username}@"
    return f"{p.protocol}://{creds}{p.host}:{p.port}"


async def resolve_proxy_pool(
    session: AsyncSession, selector: str | None, fallback_proxy_id: int | None = None,
) -> list[str | None]:
    """По селектору прогона (+ fallback proxy_id) вернуть список proxy-URL (или
    [None] для direct). Вызывающий делает random-rotation по нему per-request,
    чтобы не упираться в один exit-IP. Общая логика для постинга и для
    перепроверки ссылок (link-check).

    Формы selector-а:
      None / "direct"     → [None]
      "all"               → все активные незалоченные
      "provider:<name>"   → активные незалоченные от провайдера <name>
      "single:<id>"       → один конкретный proxy
    Пустой пул / невалидный selector → [None] (direct).
    """
    sel = (selector or "").strip()

    if sel in ("", "direct"):
        if fallback_proxy_id and not sel:
            px = await session.scalar(select(Proxy).where(Proxy.id == fallback_proxy_id))
            if px and px.is_active:
                return [proxy_url(px)]
        return [None]

    # is_active И (locked_until IS NULL ИЛИ <= now): автолоченные (накопили
    # network-fail-ы) пропускаем, после cooldown вернутся.
    now = datetime.now(UTC)
    unlocked_pred = (Proxy.locked_until.is_(None)) | (Proxy.locked_until <= now)
    if sel == "all":
        rows = (await session.execute(
            select(Proxy).where(Proxy.is_active.is_(True), unlocked_pred)
        )).scalars().all()
    elif sel.startswith("provider:"):
        provider = sel.removeprefix("provider:").strip()
        rows = (await session.execute(
            select(Proxy).where(
                Proxy.is_active.is_(True), unlocked_pred,
                (Proxy.source == provider) | (Proxy.provider == provider),
            )
        )).scalars().all()
    elif sel.startswith("single:"):
        try:
            pid = int(sel.removeprefix("single:"))
        except ValueError:
            return [None]
        px = await session.scalar(select(Proxy).where(Proxy.id == pid))
        if px and px.locked_until and px.locked_until > now:
            log.warning("proxy.single_locked_ignored", proxy_id=pid, locked_until=str(px.locked_until))
        return [proxy_url(px)] if px and px.is_active else [None]
    else:
        log.warning("proxy.unknown_selector", selector=sel)
        return [None]

    urls = [proxy_url(p) for p in rows if p.is_active]
    if not urls:
        log.warning("proxy.empty_pool", selector=sel)
        return [None]
    return urls


async def _ping_proxy_url(url: str, timeout: float = 6.0) -> bool:
    """Лёгкий пинг прокси через ipify (без БД). True — прокси ответил рабочим IP."""
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout) as c:
            r = await c.get("https://api.ipify.org?format=json")
            return bool((r.json() or {}).get("ip"))
    except Exception:
        return False


async def preflight_pool_alive(
    proxy_urls: list[str | None],
    *,
    sample: int = 8,
    timeout: float = 6.0,
    dead_ratio: float = 0.7,
) -> bool:
    """
    Грубая проверка «пул в основном жив?» ПЕРЕД стартом рана — защита от дурака
    (например, прокси добавили, но забыли оплатить → все дохлые).

    Пингует сэмпл прокси через ipify. Возвращает False, если ≥ dead_ratio сэмпла
    мертвы → вызывающий уходит в direct, чтобы не грайндить дохлыми прокси и не
    рикошетить по сайтам. Пустой / только-direct пул → True (проверять нечего).

    Majority-vote (порог 70%): одиночный fail (ipify моргнул на одном exit) не
    роняет живой пул. Любая ошибка самой проверки → True (fail-open, не ломаем
    старт рана — за частичную смерть отвечает штатный per-item лок прокси).
    """
    reals = [u for u in proxy_urls if u]
    if not reals:
        return True
    try:
        pick = reals if len(reals) <= sample else random.sample(reals, sample)
        results = await asyncio.gather(*(_ping_proxy_url(u, timeout) for u in pick))
        dead = sum(1 for ok in results if not ok)
        alive = (dead / len(pick)) < dead_ratio
        if not alive:
            log.warning("proxy.preflight.pool_dead", sampled=len(pick), dead=dead)
        return alive
    except Exception as e:  # noqa: BLE001
        log.warning("proxy.preflight.error", error=str(e)[:200])
        return True


async def pick_active_proxy_url(session: AsyncSession) -> str | None:
    """Случайный активный residential/mobile прокси-URL — для Tier2/CF-путей
    (provision / links / post-ops), где curl_cffi должен идти через чистый exit
    IP (CF режет datacenter по репутации). None → пул пуст, идём direct."""
    p = await session.scalar(
        select(Proxy)
        .where(Proxy.is_active.is_(True),
               Proxy.proxy_type.in_(("residential", "mobile")))
        .order_by(func.random())
        .limit(1)
    )
    return proxy_url(p) if p else None


async def _ip_api_lookup(client: httpx.AsyncClient, ip: str) -> dict:
    """ip-api.com lookup — free 45 req/min, IP-info без auth."""
    try:
        resp = await client.get(
            f"http://ip-api.com/json/{ip}"
            "?fields=status,countryCode,country,isp,org,as,hosting,proxy,mobile",
            timeout=10,
        )
        if resp.status_code != 200:
            return {}
        info = resp.json()
        if (info or {}).get("status") != "success":
            return {}
        ptype = "datacenter" if info.get("hosting") else (
            "mobile" if info.get("mobile") else (
                "proxy" if info.get("proxy") else "residential"
            )
        )
        return {
            "country": (info.get("countryCode") or "")[:10] or None,
            "isp": ((info.get("isp") or info.get("org") or "") or "")[:255] or None,
            "asn": (info.get("as") or "")[:255] or None,
            "proxy_type": ptype,
        }
    except Exception as e:
        log.warning("proxies.ipapi.lookup_failed", ip=ip, error=str(e))
        return {}


async def check_proxy(session: AsyncSession, proxy_id: int) -> dict:
    """
    Подключиться через proxy → GET ipify → IP → ip-api lookup.
    Обновить proxy row.
    """
    p = await get_proxy(session, proxy_id)
    if p is None:
        return {"ok": False, "error": "not found"}

    url = proxy_url(p)
    values: dict = {"last_checked_at": datetime.now(UTC)}

    # 1. ipify probe через proxy
    external_ip: str | None = None
    err: str | None = None
    try:
        async with httpx.AsyncClient(proxy=url, timeout=15) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            external_ip = (resp.json() or {}).get("ip")
            if not external_ip:
                err = "ipify returned no IP"
    except Exception as e:
        err = str(e)[:200]

    if err or not external_ip:
        values.update({
            "status": "down",
            "last_check_error": err or "unknown",
        })
        await session.execute(update(Proxy).where(Proxy.id == proxy_id).values(**values))
        await session.commit()
        return {"ok": False, "error": err, "external_ip": None}

    # 2. ip-api enrichment
    async with httpx.AsyncClient() as plain:
        meta = await _ip_api_lookup(plain, external_ip)

    values.update({
        "status": "active",
        "last_check_error": None,
        "external_ip": external_ip,
        # Прокси ответил → сбрасываем health-счётчики и снимаем lock.
        "consecutive_failures": 0,
        "locked_until": None,
        **{k: v for k, v in meta.items() if v is not None},
    })
    await session.execute(update(Proxy).where(Proxy.id == proxy_id).values(**values))
    await session.commit()
    return {"ok": True, "external_ip": external_ip, **meta}


async def recheck_all_proxies(
    session: AsyncSession, *, only_active: bool = False, concurrency: int = 10
) -> dict:
    """Health-check всех проксей пачками. Используется daily-cron-ом и кнопкой
    «recheck all». Оживший прокси разлочивается (check_proxy сбрасывает
    счётчики), мёртвый помечается status='down'.

    only_active=True — проверяем только is_active (для daily, экономим запросы).
    """
    import asyncio

    from core.db import WriteSession

    stmt = select(Proxy.id)
    if only_active:
        stmt = stmt.where(Proxy.is_active.is_(True))
    ids = [int(r) for r in (await session.execute(stmt)).scalars().all()]

    counters = {"ok": 0, "down": 0}
    sem = asyncio.Semaphore(concurrency)

    async def _one(pid: int) -> None:
        async with sem:
            async with WriteSession() as s:
                try:
                    res = await check_proxy(s, pid)
                    counters["ok" if res.get("ok") else "down"] += 1
                except Exception as e:
                    counters["down"] += 1
                    log.warning("proxies.recheck.error", proxy_id=pid, error=str(e))

    await asyncio.gather(*[_one(pid) for pid in ids])
    log.info(
        "proxies.recheck_all.done",
        total=len(ids), ok=counters["ok"], down=counters["down"], only_active=only_active,
    )
    return {"total": len(ids), **counters}


# ─── Health auto-lock (см. миграцию 0024) ──────────────────────────

PROXY_FAILURE_THRESHOLD = 5         # network-fail-ов до lock
PROXY_LOCK_DURATION_MINUTES = 30    # cooldown окно


async def report_proxy_failure(session: AsyncSession, proxy_id: int) -> None:
    """Bump failures counter. При достижении threshold — лочит на 30 мин.

    Вызывается из posting/validator при httpx.NetworkError / ConnectTimeout /
    proxy-specific ошибках. НЕ вызывается на HTTP 4xx/5xx от целевого сайта —
    эти ошибки про сайт, не про прокси.
    """
    from datetime import timedelta
    from sqlalchemy import case

    now = datetime.now(UTC)
    # Atomic: counter++, if reaches threshold → lock. CASE-выражение, без race.
    new_counter_expr = Proxy.consecutive_failures + 1
    new_lock_expr = case(
        (new_counter_expr >= PROXY_FAILURE_THRESHOLD,
         now + timedelta(minutes=PROXY_LOCK_DURATION_MINUTES)),
        else_=Proxy.locked_until,
    )
    await session.execute(
        update(Proxy).where(Proxy.id == proxy_id).values(
            consecutive_failures=new_counter_expr,
            last_failure_at=now,
            locked_until=new_lock_expr,
        )
    )
    await session.commit()


async def report_proxy_success(session: AsyncSession, proxy_id: int) -> None:
    """Сбросить failure counter — прокси ответила успешно."""
    await session.execute(
        update(Proxy).where(Proxy.id == proxy_id, Proxy.consecutive_failures > 0)
        .values(consecutive_failures=0, locked_until=None)
    )
    await session.commit()


async def report_proxy_failure_by_url(session: AsyncSession, proxy_url: str | None) -> None:
    """То же что `report_proxy_failure`, но lookup по URL (worker не хранит id).

    Cost: один `SELECT` по индексу (host+port). Reasonable т.к. fail редок.
    """
    if not proxy_url:
        return
    # Парсим: http://[user:pass@]host:port → (host, port)
    from urllib.parse import urlparse
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return
    except Exception:
        return
    proxy_id = await session.scalar(
        select(Proxy.id).where(Proxy.host == host, Proxy.port == port).limit(1)
    )
    if proxy_id:
        await report_proxy_failure(session, int(proxy_id))
