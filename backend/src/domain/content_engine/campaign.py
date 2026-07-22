"""Content Engine: генерация csv_campaign-рана (C2, отдельная полоса).

generate_campaign_run(run_id): читает gen_params (rows, content_mode,
prompt_template_id, ai_model_id, language), генерит тексты моделью и наполняет
texts + text_items. Режимы:
  • gen_per_post — на каждое из count размещений свой уникальный текст;
  • gen_per_row  — 1 оригинал на строку + спин-расшивка на count (через
                   fanout_materialize); оригинал reusable.
По завершении: auto → ставим в очередь постинга; manual → READY (ревью → Start).

Генерация ограничена общим gen-лимитером (LLM-rate), не зависит от постинга.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, or_, select, text as sql, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import WriteSession
from domain.ai import GenerationError, generate_text, pick_model, render_prompt
from domain.content_engine.service import fanout_materialize, make_variant
from domain.text_links import normalize_domain, sanitize_text_html
from domain.texts import create_texts
from infrastructure.concurrency import RedisConcurrencyLimiter
from infrastructure.db.models import (
    AiModel, PostingRun, PostingRunStatus, PromptTemplate, Text, TextItem, TextItemStatus,
)

log = structlog.get_logger(__name__)

# Абсолютный жёсткий потолок одновременных LLM-вызовов (защита от кривой настройки).
# Реальная конц*  берётся из AppSettings.content_gen_concurrency (рантайм-тюнинг).
GEN_MAX_CONCURRENCY = int(os.getenv("GEN_MAX_CONCURRENCY", "50"))
# Жёсткий per-item таймаут на генерацию (AI-вызов). Зависший запрос к модели без
# своего таймаута иначе вешает _gen_one → gather ждёт вечно → генерация «встаёт»
# (gen_active висит, ран не добирает тексты). При таймауте айтем остаётся
# несгенерированным (text_id NULL) → перегенерится на следующем проходе.
GEN_ITEM_TIMEOUT_S = float(os.getenv("GEN_ITEM_TIMEOUT_S", "180"))
gen_limiter = RedisConcurrencyLimiter("generation", stale_ttl_s=300.0)

# content_gen_concurrency читается на КАЖДЫЙ LLM-вызов (_gen) и на старте bulk —
# кешируем на 15с, чтобы не бить БД. Рантайм-правка подхватывается в пределах ~15с.
_gen_conc_cache: dict = {"val": None, "ts": 0.0}


async def _gen_concurrency() -> int:
    """Текущий потолок одновременной генерации (AppSettings, кеш 15с, hard-cap)."""
    now = time.monotonic()
    if _gen_conc_cache["val"] is None or now - _gen_conc_cache["ts"] > 15:
        from domain.app_settings.service import get_app_settings
        async with WriteSession() as s:
            val = int((await get_app_settings(s)).content_gen_concurrency)
        _gen_conc_cache["val"] = max(1, min(val, GEN_MAX_CONCURRENCY))
        _gen_conc_cache["ts"] = now
    return _gen_conc_cache["val"]

_SPIN_PROMPT = (
    "Rewrite the text in spintax format: wrap synonym groups in {{}} separated by |, "
    "keep meaning, keep HTML tags and the link unchanged. Don't rewrite: {stop}.\n\nText:\n{text}"
)


def _row_vars(row: dict, language: str | None) -> dict:
    v = {"keyword": row.get("keyword") or "", "link": row.get("link") or "",
         "links": row.get("link") or "", "anchor": row.get("anchor") or "",
         "language": row.get("language") or language or "en"}
    cp = row.get("content_parametrs") or {}
    if isinstance(cp, dict):
        v.update({k: str(val) for k, val in cp.items()})
    return v


async def _gen(model: AiModel, prompt: str) -> str:
    """Один генерационный вызов под gen-лимитером (глобальный потолок =
    content_gen_concurrency, тюнится без рестарта)."""
    async with gen_limiter.slot(limit=await _gen_concurrency()):
        async with WriteSession() as s:
            return await generate_text(s, model=model, prompt=prompt)


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TEXT_RE = re.compile(r"<text>(.*?)</text>", re.IGNORECASE | re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _parse_generated(raw: str) -> tuple[str | None, str]:
    """AI-ответ формата <title>…</title><text>…</text> → (title, body).
    Если тегов нет — (None, raw). Тело — содержимое <text> (без обёртки),
    title-тег из тела вырезаем."""
    raw = (raw or "").strip()
    raw = _FENCE_RE.sub("", raw).strip()  # снимаем ```html … ``` если есть
    mt_title = _TITLE_RE.search(raw)
    title = (mt_title.group(1).strip() or None) if mt_title else None
    mt_text = _TEXT_RE.search(raw)
    if mt_text:
        body = mt_text.group(1).strip()
    else:
        # нет <text> — берём всё, но вычищаем title-тег из тела
        body = _TITLE_RE.sub("", raw).strip()
    # Санитизация AI-вывода: чиним битый HTML (некавыченные/незакрытые теги,
    # хвостовые кавычки в href) сразу при генерации.
    body = sanitize_text_html(body) or body
    return title, body


async def create_campaign_run(
    session: AsyncSession, *, project_id: int, creator_id: int | None, name: str,
    rows: list[dict], content_mode: str, run_mode: str = "manual",
    prompt_template_id: int | None = None, ai_model_id: int | None = None,
    language: str | None = None, scheduled_for=None, spread_days: int = 0,
    proxy_selector: str | None = None, posting_method: str = "auto",
    priority: str = "normal", post_verify: str = "mark",
) -> PostingRun:
    """Создать csv_campaign-ран (status=UNPACKING). Генерацию запускает TaskIQ
    generate_campaign (caller дёргает после commit)."""
    if not rows:
        raise ValueError("нет строк кампании")
    if content_mode not in ("gen_per_post", "gen_per_row", "reuse"):
        raise ValueError(f"bad content_mode {content_mode}")
    total = sum(max(1, int(r.get("count") or 1)) for r in rows)
    run = PostingRun(
        project_id=project_id, created_by=creator_id, name=name.strip(),
        status=PostingRunStatus.UNPACKING.value, task_type="post",
        content_source="csv_campaign", content_mode=content_mode, run_mode=run_mode,
        scheduled_for=scheduled_for, spread_days=spread_days,
        proxy_selector=proxy_selector, posting_method=posting_method, priority=priority,
        post_verify=post_verify, total_texts=total,
        gen_params={"rows": rows, "prompt_template_id": prompt_template_id,
                    "ai_model_id": ai_model_id, "language": language},
    )
    session.add(run)
    await session.commit()
    # Авто-привязка целевых доменов задачи к проекту («забыл добавить» safety-net):
    # колонка link → project_domains. Идемпотентно + резолвит needs_review-txt.
    from domain.project_domains import autobind_link_domains
    await autobind_link_domains(session, project_id, [r.get("link") for r in rows])
    return run


async def _gen_progress(run_id: int, *, done: int | None = None, total: int | None = None,
                        main_ids: list[int] | None = None) -> None:
    """Записать прогресс генерации в gen_params (gen_done/gen_total + опц.
    main_text_ids). Питает красный бар генерации в UI/Global Queue, а инкремент
    main_text_ids — живой показ оригиналов gen_per_row по мере генерации.
    jsonb_set точечно — не затираем остальные ключи. Во время UNPACKING писатель
    один (gen-таск)."""
    expr = "coalesce(gen_params, '{}'::jsonb)"
    params: dict = {"r": run_id}
    if total is not None:
        expr = f"jsonb_set({expr}, '{{gen_total}}', to_jsonb(cast(:total as integer)))"
        params["total"] = total
    if done is not None:
        expr = f"jsonb_set({expr}, '{{gen_done}}', to_jsonb(cast(:done as integer)))"
        params["done"] = done
    if main_ids is not None:
        expr = f"jsonb_set({expr}, '{{main_text_ids}}', cast(:mids as jsonb))"
        params["mids"] = json.dumps(main_ids)
    if len(params) == 1:  # нечего писать
        return
    async with WriteSession() as s:
        await s.execute(sql(f"UPDATE posting_runs SET gen_params = {expr} WHERE id=:r"), params)
        await s.commit()


async def set_gen_active(run_id: int, active: bool) -> None:
    """Флаг «генерация идёт прямо сейчас» в gen_params. Стрим-постинг (manual
    gen_per_post): пока флаг true и есть несгенерённые айтемы — постинг ждёт
    новые готовые тексты, не финишируя. Ставится на входе генерации, снимается
    в finally (и при kill подчистит recover_stalled_runs / stall-кап постинга)."""
    async with WriteSession() as s:
        await s.execute(sql(
            "UPDATE posting_runs SET gen_params = jsonb_set("
            "coalesce(gen_params,'{}'::jsonb), '{gen_active}', to_jsonb(cast(:a as boolean))) "
            "WHERE id=:r"), {"r": run_id, "a": active})
        await s.commit()


async def generate_campaign_run(run_id: int) -> dict:
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        gp = run.gen_params or {}
        rows = gp.get("rows") or []
        mode = run.content_mode or "gen_per_post"
        language = gp.get("language")
        manual = run.run_mode == "manual"
        content_model = await pick_model(s, purpose="content", model_pk=gp.get("ai_model_id"))
        # spin-модель тут больше не нужна: спинтакс делаем на Start (fanout)
        tpl = None
        if gp.get("prompt_template_id"):
            tpl = await s.scalar(select(PromptTemplate).where(
                PromptTemplate.id == gp["prompt_template_id"]))
    if not rows:
        return {"ok": False, "error": "no rows"}

    # ─── reuse: без AI, берём reusable-оригиналы из библиотеки ───
    if mode == "reuse":
        from domain.reuse import generate_reuse_items
        async with WriteSession() as s:
            total = await generate_reuse_items(
                s, run_id=run_id, project_id=run.project_id, tasks=rows, lang=language)
        if total == 0:
            await _fail_run(run_id, "нет reusable-текстов (reusable + spin_formula + запас лимита)")
            return {"ok": False, "error": "no reusable texts"}
        await _gen_progress(run_id, done=total, total=total)  # reuse мгновенный → 100%
        await _finalize_run(run_id, total, manual, [])
        log.info("content_engine.campaign.reused", run_id=run_id, items=total)
        return {"ok": True, "items": total, "mode": mode, "manual": manual}

    if content_model is None:
        await _fail_run(run_id, "нет активной content-модели (AI Providers)")
        return {"ok": False, "error": "no content model"}
    tpl_body = tpl.body if tpl else "{keyword}"

    try:
        # ─── gen_per_post: уникальный текст на каждый из count постов ───
        if mode == "gen_per_post":
            total_items = 0
            gen_n = 0
            gen_total = sum(max(1, int(r.get("count") or 1)) for r in rows)
            await _gen_progress(run_id, done=0, total=gen_total)
            for row in rows:
                count = max(1, int(row.get("count") or 1))
                prompt = render_prompt(tpl_body, _row_vars(row, language))
                td = normalize_domain(row.get("link") or "")
                row_lang = row.get("language") or language  # язык файла важнее формы
                for _ in range(count):
                    raw = await _gen(content_model, prompt)
                    title, body = _parse_generated(raw)  # <title>/<text> → title + чистое тело
                    total_items += await _materialize_one(
                        run_id, run.project_id, body, row, td, content_model,
                        reusable=True, lang=row_lang, title=title)
                    gen_n += 1
                    await _gen_progress(run_id, done=gen_n)  # красный бар генерации
            await _finalize_run(run_id, total_items, manual, [])
            log.info("content_engine.campaign.generated", run_id=run_id, items=total_items, mode=mode)
            return {"ok": True, "items": total_items, "mode": mode, "manual": manual}

        # ─── gen_per_row: оригинал-плайн на строку + СРАЗУ N text_items ───
        # item[0] = оригинал (с текстом, для ревью в обычном редакторе);
        # item[1..] = пустые (text_id=NULL), заполнятся спинами на Start.
        groups: list[dict] = []   # [{text_id, link, anchor, count, original_item_id, spin_item_ids}]
        main_ids: list[int] = []
        planned = 0
        await _gen_progress(run_id, done=0, total=len(rows))  # 1 AI-оригинал на строку
        for row in rows:
            count = max(1, int(row.get("count") or 1))
            prompt = render_prompt(tpl_body, _row_vars(row, language))
            raw = await _gen(content_model, prompt)
            title, body = _parse_generated(raw)  # <title>/<text> → title + чистое тело
            link, anchor = row.get("link"), (row.get("anchor") or "")
            async with WriteSession() as s:
                orig = Text(body=body, spin_formula=None, title=title,
                            lang=(row.get("language") or language),
                            source="generated", gen_model=content_model.model_id,
                            reusable=True, content_hash=_hash(body))
                s.add(orig); await s.flush()
                oid = orig.id
                grp_items = await _create_group_items(
                    s, run_id, run.project_id, orig, link, anchor, count, row=row)
                await s.commit()
            main_ids.append(oid)
            groups.append({"text_id": oid, "link": link, "anchor": anchor,
                           "count": count, **grp_items})
            planned += count
            # красный бар + ЖИВОЙ показ оригиналов (main_text_ids инкрементально)
            await _gen_progress(run_id, done=len(main_ids), main_ids=main_ids)
    except GenerationError as e:
        await _fail_run(run_id, f"generation failed: {e}")
        return {"ok": False, "error": str(e)}

    if manual:
        # item-ы уже созданы (оригиналы + пустые) → READY; спин-заполнение на Start
        async with WriteSession() as s:
            gp2 = dict((await s.scalar(select(PostingRun.gen_params).where(PostingRun.id == run_id))) or {})
            gp2.pop("error", None)  # успешная регенерация — снимаем прошлую ошибку
            gp2.update({"main_text_ids": main_ids, "fanout_groups": groups, "deferred_fanout": True})
            await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(
                gen_params=gp2, total_texts=planned, status=PostingRunStatus.READY.value))
            await s.commit()
        log.info("content_engine.campaign.gen_per_row.ready", run_id=run_id,
                 originals=len(main_ids), items=planned)
        return {"ok": True, "items": planned, "originals": len(main_ids), "planned": planned,
                "mode": mode, "manual": True}

    # auto: сохраняем группы, заполняем спинами сразу + в очередь постинга
    async with WriteSession() as s:
        gp2 = dict((await s.scalar(select(PostingRun.gen_params).where(PostingRun.id == run_id))) or {})
        gp2.update({"main_text_ids": main_ids, "fanout_groups": groups})
        await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(gen_params=gp2))
        await s.commit()
    total = await _fill_campaign_groups(run_id)
    await _finalize_run(run_id, total, False, main_ids)
    log.info("content_engine.campaign.generated", run_id=run_id, items=total, mode=mode)
    return {"ok": True, "items": total, "mode": mode, "manual": False}


async def _create_group_items(s, run_id: int, project_id: int, orig: Text,
                              link, anchor: str, count: int, row: dict | None = None,
                              empty: bool = False) -> dict:
    """Создать count text_items для строки gen_per_row:
    item[0] = оригинал, item[1..] = спины (text_id=NULL, заполнятся на Start).
    Возвращает id-шники.

    `row` — gen-контекст строки (keyword/lang/link/anchor) для пер-айтем регена.
    `empty=True` (manual «не генерим сразу») — оригинал-айтем тоже без текста
    (text_id=NULL), наполнится по «Сгенерировать»; orig — пустой плейсхолдер."""
    td = normalize_domain(link or "")
    obody = orig.body or ""
    anchor_v = (anchor or None) and anchor[:500]
    item_rows: list[dict] = [{
        "posting_run_id": run_id, "project_id": project_id,
        "text_id": None if empty else orig.id,
        "original_filename": (orig.title or f"text-{orig.id}")[:500],
        "title": orig.title, "content_hash": orig.content_hash,
        "byte_size": (0 if empty else len(obody.encode("utf-8"))),
        "status": TextItemStatus.PENDING.value,
        "link_url": link, "link_anchor": anchor_v, "target_domain": td, "lang": orig.lang,
        "gen_row": row,
    }]
    for k in range(1, count):
        item_rows.append({
            "posting_run_id": run_id, "project_id": project_id,
            "text_id": None, "original_filename": "(спин)", "title": orig.title,
            "content_hash": hashlib.sha256(f"spin-{run_id}-{orig.id}-{k}".encode()).hexdigest(),
            "byte_size": 0, "status": TextItemStatus.PENDING.value,
            "link_url": link, "link_anchor": anchor_v, "target_domain": td, "lang": orig.lang,
            "gen_row": row,
        })
    res = await s.execute(TextItem.__table__.insert().returning(TextItem.id), item_rows)
    ids = [int(r[0]) for r in res.all()]
    return {"original_item_id": ids[0], "spin_item_ids": ids[1:]}


async def _fill_campaign_groups(run_id: int) -> int:
    """Start (новая модель): заполнить уже созданные item-ы. item[0] → тело
    оригинала + чистая ссылка (без спина); item[1..] → уникальные спины оригинала
    + ссылка. Спинтакс генерим здесь из (отревьюенного) тела. Возвращает total."""
    async with WriteSession() as s0:
        spin_model = await pick_model(s0, purpose="spin")
        run = await s0.scalar(select(PostingRun).where(PostingRun.id == run_id))
        groups = (run.gen_params or {}).get("fanout_groups") or []
    total = 0
    for g in groups:
        async with WriteSession() as s:
            total += await _fanout_one_group(s, g, spin_model)
            await s.commit()
    return total


async def _fanout_one_group(s, g: dict, spin_model) -> int:
    """Заполнить ВСЕ айтемы группы финальными текстами: оригинал (тело + чистая
    ссылка), спины (spin + ссылка). Деривация spin_formula при нужде. Делается
    в ОДНОЙ сессии (атомарный commit снаружи) — чтобы стриминг-постинг не схватил
    полу-готовую группу. Возвращает число заполненных айтемов."""
    orig = await s.scalar(select(Text).where(Text.id == g.get("text_id")))
    if orig is None or not (orig.body or "").strip():
        return 0  # оригинал ещё не сгенерён (пустое тело) → нечего расшивать
    # Идемпотентность: после расшивки оригинал-айтем указывает на ВАРИАНТ (не на
    # исходный Text). Если уже так — группа расшита (повторный «Заполнить спины»
    # либо Start после fill-spins) → пропускаем, не плодим дубли.
    orig_item_tid = await s.scalar(
        select(TextItem.text_id).where(TextItem.id == g.get("original_item_id")))
    if orig_item_tid is not None and orig_item_tid != orig.id:
        return 0
    link, anchor = g.get("link"), (g.get("anchor") or "")
    spin_formula = orig.spin_formula
    if spin_model and not spin_formula:
        try:
            sp = await _gen(spin_model, _SPIN_PROMPT
                .replace("{stop}", anchor).replace("{text}", orig.body or ""))
            if sp and sp.strip():
                spin_formula = sp.strip()
                orig.spin_formula = spin_formula
        except GenerationError:
            pass
    # (item_id, body): item[0] — оригинал (без спина), item[1..] — спины
    jobs: list[tuple[int, str]] = [
        (g["original_item_id"], make_variant(orig.body, None, link, anchor))]
    for sid in (g.get("spin_item_ids") or []):
        jobs.append((sid, make_variant(orig.body, spin_formula, link, anchor)))
    text_rows = [{
        "body": body, "title": orig.title, "lang": orig.lang,
        "source": "spin_variant", "gen_model": orig.gen_model,
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "parent_text_id": orig.id, "reusable": False,
    } for (_iid, body) in jobs]
    vids = await create_texts(s, text_rows)
    for (iid, body), vid in zip(jobs, vids, strict=True):
        await s.execute(update(TextItem).where(TextItem.id == iid).values(
            text_id=vid, byte_size=len(body.encode("utf-8")), title=orig.title,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            status=TextItemStatus.PENDING.value))
    await s.execute(update(Text).where(Text.id == orig.id).values(
        used_as_original=True, times_used=Text.times_used + len(jobs),
        last_used_at=datetime.now(UTC)))
    return len(jobs)


async def _fanout_campaign_groups(run_id: int, project_id: int, groups: list[dict]) -> int:
    """[Legacy, старые отложенные раны без item-ов] Расшить оригинал в count
    вариантов (spin + inject) → НОВЫЕ text_items.

    Спинтакс генерим ЗДЕСЬ (на Start) из отревьюенного тела оригинала — чтобы
    юзер сначала проверял обычный текст, а вариативность добавлялась уже после
    его approve. Нет spin-модели → размещения = тело как есть + инжект ссылки.
    """
    async with WriteSession() as s0:
        spin_model = await pick_model(s0, purpose="spin")
    total = 0
    for g in groups:
        cnt = max(1, int(g.get("count") or 1))
        async with WriteSession() as s:
            orig = await s.scalar(select(Text).where(Text.id == g.get("text_id")))
            if orig is None:
                continue
            # спинтакс из отревьюенного тела (если есть spin-модель и ещё не делали)
            if spin_model and not orig.spin_formula:
                try:
                    sp = await _gen(spin_model, _SPIN_PROMPT
                        .replace("{stop}", g.get("anchor") or "").replace("{text}", orig.body or ""))
                    if sp and sp.strip():
                        orig.spin_formula = sp.strip()  # закоммитит fanout_materialize
                except GenerationError:
                    pass
            ids = await fanout_materialize(
                s, run_id=run_id, project_id=project_id, original=orig,
                placements=[{"link": g.get("link"), "anchor": g.get("anchor") or ""}] * cnt)
            total += len(ids)
    return total


async def start_campaign_fanout(run_id: int) -> dict:
    """Start для gen_per_row: заполнить/расшить оригиналы в text_items + drip по
    spread_days + в очередь Celery. Идемпотентно по статусу (после Start → QUEUED).

    Новая модель (item-ы уже созданы при генерации) → `_fill_campaign_groups`.
    Старые отложенные раны (без item-ов) → legacy `_fanout_campaign_groups`.
    """
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "run not found"}
        gp = run.gen_params or {}
        groups = gp.get("fanout_groups") or []
        if not groups:
            return {"ok": False, "error": "nothing to fanout"}
        if run.status not in (PostingRunStatus.READY.value, PostingRunStatus.SCHEDULED.value,
                              PostingRunStatus.DRAFT.value):
            return {"ok": True, "status": "already_started"}
        project_id, spread_days = run.project_id, (run.spread_days or 0)
        scheduled_for, priority = run.scheduled_for, run.priority
        new_model = bool(groups[0].get("original_item_id"))

    if new_model:
        # Идемпотентно: уже расшитые группы (через «Заполнить спины») пропустятся.
        await _fill_campaign_groups(run_id)
    else:
        await _fanout_campaign_groups(run_id, project_id, groups)

    async with WriteSession() as s:
        # total_texts — фактическое число айтемов (а не возврат fanout: при
        # предварительном fill-spins расшивка уже сделана и вернёт 0).
        total = await s.scalar(select(func.count(TextItem.id))
                               .where(TextItem.posting_run_id == run_id)) or 0
        if spread_days and spread_days > 0:
            now_ts = datetime.now(UTC)
            ws = scheduled_for if (scheduled_for and scheduled_for > now_ts) else now_ts
            await s.execute(sql("""
                WITH o AS (SELECT id, (row_number() OVER (ORDER BY id)-1)::float AS rn,
                                  GREATEST(count(*) OVER ()-1,1)::float AS denom
                           FROM text_items WHERE posting_run_id=:rid)
                UPDATE text_items t SET not_before=(:ws)::timestamptz
                       + make_interval(secs=>(o.rn/o.denom)*:win)
                FROM o WHERE t.id=o.id
            """), {"rid": run_id, "ws": ws, "win": spread_days * 86400})
        await s.execute(update(PostingRun).where(PostingRun.id == run_id)
                        .values(status=PostingRunStatus.QUEUED.value, total_texts=total))
        await s.commit()

    from core.celery_app import celery_app
    from infrastructure.db.models import CELERY_PRIORITY_MAP
    celery_app.send_task("postings.run_posting", args=[run_id],
                         priority=CELERY_PRIORITY_MAP.get(priority or "normal", 5))
    log.info("content_engine.campaign.fanout_started", run_id=run_id, items=total)
    return {"ok": True, "status": "queued", "items": total}


async def fill_campaign_spins(run_id: int) -> dict:
    """Manual «Заполнить спины»: расшить готовые оригиналы gen_per_row в финальные
    постабельные тексты (оригинал с инжектом ссылки + спин-варианты) БЕЗ старта
    постинга. Ран остаётся READY — спины видны в таблице для ревью перед Start.

    Идемпотентно: расшивает только группы, чей оригинал уже сгенерён и ещё не
    расшит (`_fanout_one_group` сам пропускает пустые/уже-расшитые). Спин-айтемы
    на время расшивки помечаются GENERATING — построчный прогресс виден в UI."""
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "not found"}
        if run.content_source != "csv_campaign" or run.content_mode != "gen_per_row":
            return {"ok": False, "error": "only gen_per_row campaigns"}
        gp = run.gen_params or {}
        groups = gp.get("fanout_groups") or []
        spin_model = await pick_model(s, purpose="spin")

    # Группы к расшивке: оригинал-айтем сгенерён (text_id == исходный Text) и ещё
    # не расшит. Собираем спин-айтемы для claim-а (видимый прогресс).
    needs: list[dict] = []
    spin_ids: list[int] = []
    ungenerated = 0
    async with WriteSession() as s:
        for g in groups:
            oi_tid = await s.scalar(select(TextItem.text_id)
                                    .where(TextItem.id == g.get("original_item_id")))
            if oi_tid is None:
                ungenerated += 1            # оригинал ещё не сгенерён → пропуск
            elif oi_tid == g.get("text_id"):
                needs.append(g)             # сгенерён, не расшит → расшиваем
                spin_ids.extend(g.get("spin_item_ids") or [])
            # else: уже расшит (указывает на вариант) → пропуск
    if not needs:
        return {"ok": True, "filled": 0, "groups": 0, "ungenerated": ungenerated,
                "note": "нет готовых оригиналов для расшивки"}

    # claim спин-айтемов → GENERATING (оранжевый спиннер построчно) + ран в
    # UNPACKING (активная обработка, оранжевый ген-бар в очереди)
    async with WriteSession() as s:
        if spin_ids:
            await s.execute(update(TextItem)
                            .where(TextItem.id.in_(spin_ids), TextItem.text_id.is_(None))
                            .values(status=TextItemStatus.GENERATING.value, last_error=None))
        await s.execute(update(PostingRun).where(PostingRun.id == run_id)
                        .values(status=PostingRunStatus.UNPACKING.value))
        await s.commit()

    await _gen_progress(run_id, done=0, total=len(needs))
    filled = 0
    for i, g in enumerate(needs, start=1):
        async with WriteSession() as s:  # атомарно: группа целиком за один commit
            filled += await _fanout_one_group(s, g, spin_model)
            await s.commit()
        await _gen_progress(run_id, done=i)

    async with WriteSession() as s:
        total = await s.scalar(select(func.count(TextItem.id))
                               .where(TextItem.posting_run_id == run_id)) or 0
        await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(
            status=PostingRunStatus.READY.value, total_texts=total))
        await s.commit()
    log.info("content_engine.fill_spins.done", run_id=run_id,
             filled=filled, groups=len(needs), ungenerated=ungenerated)
    return {"ok": True, "filled": filled, "groups": len(needs), "ungenerated": ungenerated}


async def _finalize_run(run_id: int, total_items: int, manual: bool, main_ids: list[int]) -> None:
    """Финал генерации: auto → очередь постинга (QUEUED + Celery); manual → READY (ревью)."""
    async with WriteSession() as s:
        gp2 = dict((await s.scalar(select(PostingRun.gen_params).where(PostingRun.id == run_id))) or {})
        gp2["main_text_ids"] = main_ids
        gp2.pop("error", None)  # успешная генерация — снимаем прошлую ошибку
        vals = {"total_texts": total_items, "gen_params": gp2,
                "status": PostingRunStatus.READY.value if manual else PostingRunStatus.QUEUED.value}
        await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(**vals))
        await s.commit()
    if not manual:
        from core.celery_app import celery_app
        from infrastructure.db.models import CELERY_PRIORITY_MAP
        async with WriteSession() as s:
            prio = await s.scalar(select(PostingRun.priority).where(PostingRun.id == run_id))
        celery_app.send_task("postings.run_posting", args=[run_id],
                             priority=CELERY_PRIORITY_MAP.get(prio or "normal", 5))


async def fill_pending_spins(limit: int = 100) -> dict:
    """Spin-воркер: заполнить spin_formula у reusable-текстов где NULL, через
    spin-модель. Без spin-модели — ничего не делаем. Идемпотентно, батчами."""
    async with WriteSession() as s:
        spin_model = await pick_model(s, purpose="spin")
        if spin_model is None:
            return {"ok": True, "filled": 0, "note": "no spin model"}
        rows = (await s.execute(
            select(Text.id, Text.body).where(
                Text.reusable.is_(True), Text.spin_formula.is_(None),
                Text.archived_at.is_(None)).order_by(Text.id).limit(limit))).all()
    filled = 0
    for tid, body in rows:
        try:
            sp = await _gen(spin_model, _SPIN_PROMPT.replace("{stop}", "").replace("{text}", body or ""))
        except GenerationError:
            continue
        if not sp or not sp.strip():
            continue
        async with WriteSession() as s:
            await s.execute(update(Text).where(Text.id == tid).values(spin_formula=sp.strip()))
            await s.commit()
        filled += 1
    if filled:
        log.info("content_engine.spin_fill", filled=filled)
    return {"ok": True, "filled": filled}


async def _materialize_one(run_id, project_id, body, row, td, model, *, reusable,
                           lang=None, title=None) -> int:
    """gen_per_post: 1 сгенерированный текст → texts + text_item (pending)."""
    if not body or not body.strip():
        return 0
    async with WriteSession() as s:
        t = Text(body=body, title=title, lang=lang, source="generated",
                 gen_model=model.model_id, reusable=reusable, content_hash=_hash(body))
        s.add(t); await s.flush()
        await s.execute(TextItem.__table__.insert(), [{
            "posting_run_id": run_id, "project_id": project_id, "text_id": t.id,
            "original_filename": f"text-{t.id}", "title": title,
            "content_hash": _hash(body),
            "byte_size": len(body.encode()), "status": TextItemStatus.PENDING.value,
            "link_url": row.get("link"), "link_anchor": (row.get("anchor") or None),
            "target_domain": td, "lang": lang, "gen_row": row,  # для пер-айтем регена
        }])
        await s.commit()
    return 1


# ─── Пер-айтем (ре)генерация текста (кнопки в таблице) ────────────────


def _classify_item(run, item_id: int) -> tuple[str, dict | None]:
    """('per_post' | 'gen_per_row_original' | 'gen_per_row_spin', group|None).
    Тип определяется по fanout_groups (оригинал/спин); иначе — per_post."""
    for g in ((run.gen_params or {}).get("fanout_groups") or []):
        if g.get("original_item_id") == item_id:
            return ("gen_per_row_original", g)
        if item_id in (g.get("spin_item_ids") or []):
            return ("gen_per_row_spin", g)
    return ("per_post", None)


async def _set_item_text(item_id: int, body: str, *, title: str | None, source: str,
                         gen_model: str | None = None, parent_text_id: int | None = None) -> None:
    """Создать новый Text и перепривязать айтем на него → PENDING."""
    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))
        vid = (await create_texts(s, [{
            "body": body, "title": title, "lang": item.lang if item else None,
            "source": source, "gen_model": gen_model, "content_hash": _hash(body),
            "parent_text_id": parent_text_id, "reusable": (source == "generated"),
        }]))[0]
        await s.execute(update(TextItem).where(TextItem.id == item_id).values(
            text_id=vid, title=title, content_hash=_hash(body),
            byte_size=len(body.encode()), status=TextItemStatus.PENDING.value,
            last_error=None))
        await s.commit()


async def _spin_one(orig_text_id: int, link, anchor: str) -> str:
    """Тело одного спин-варианта оригинала (деривация spin_formula при нужде)."""
    async with WriteSession() as s:
        orig = await s.scalar(select(Text).where(Text.id == orig_text_id))
        if orig is None:
            return ""
        spin_formula, obody = orig.spin_formula, (orig.body or "")
    if not spin_formula:
        async with WriteSession() as s:
            spin_model = await pick_model(s, purpose="spin")
        if spin_model:
            try:
                sp = await _gen(spin_model, _SPIN_PROMPT
                                .replace("{stop}", anchor or "").replace("{text}", obody))
                if sp and sp.strip():
                    spin_formula = sp.strip()
                    async with WriteSession() as s:
                        await s.execute(update(Text).where(Text.id == orig_text_id)
                                        .values(spin_formula=spin_formula))
                        await s.commit()
            except GenerationError:
                pass
    return make_variant(obody, spin_formula, link, anchor)


async def _item_gen_failed(item_id: int, msg: str) -> None:
    """Ошибка генерации: last_error + статус (PENDING если текст уже был, иначе FAILED)."""
    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))
        st = (TextItemStatus.PENDING.value if (item and item.text_id)
              else TextItemStatus.FAILED.value)
        await s.execute(update(TextItem).where(TextItem.id == item_id)
                        .values(status=st, last_error=msg[:300]))
        await s.commit()


async def generate_item(item_id: int, *, regenerate: bool = False) -> dict:
    """Пер-айтем (ре)генерация текста для csv_campaign. Различает:
    per_post (AI с нуля), gen_per_row оригинал (AI → тело оригинала + сброс
    spin_formula), gen_per_row спин (переспин оригинала, без AI)."""
    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == item_id))
        if item is None:
            return {"ok": False, "status": "not_found"}
        run = await s.scalar(select(PostingRun).where(PostingRun.id == item.posting_run_id))
        if run is None or run.content_source != "csv_campaign":
            return {"ok": False, "status": "not_a_gen_run"}
        # GENERATING — это claim (поставлен эндпоинтом перед enqueue или нами же
        # ниже); НЕ бейлим на нём, иначе таск сам себя отклонит. Бейлим только на
        # реально несовместимых: уже постится/запощено.
        if item.status in (TextItemStatus.POSTING.value, TextItemStatus.POSTED.value):
            return {"ok": False, "status": f"busy:{item.status}"}
        if item.text_id is not None and not regenerate:
            return {"ok": False, "status": "already_has_text"}
        kind, group = _classify_item(run, item_id)
        gp = run.gen_params or {}
        language, model_pk, tpl_id = gp.get("language"), gp.get("ai_model_id"), gp.get("prompt_template_id")
        row = item.gen_row or {"link": item.link_url, "anchor": item.link_anchor}
        link, anchor = item.link_url, (item.link_anchor or "")
        group_text_id = group.get("text_id") if group else None
        # claim
        await s.execute(update(TextItem).where(TextItem.id == item_id)
                        .values(status=TextItemStatus.GENERATING.value, last_error=None))
        await s.commit()

    try:
        # ── gen_per_row спин: переспин без AI ──
        if kind == "gen_per_row_spin":
            body = await _spin_one(group_text_id, link, anchor)
            await _set_item_text(item_id, body, title=None, source="spin_variant",
                                 parent_text_id=group_text_id)
            return {"ok": True, "status": "generated", "kind": kind}

        # ── AI: per_post или оригинал gen_per_row ──
        async with WriteSession() as s:
            model = await pick_model(s, purpose="content", model_pk=model_pk)
            tpl = (await s.scalar(select(PromptTemplate).where(PromptTemplate.id == tpl_id))
                   if tpl_id else None)
        if model is None:
            await _item_gen_failed(item_id, "нет активной content-модели")
            return {"ok": False, "status": "no_model"}
        prompt = render_prompt(tpl.body if tpl else "{keyword}", _row_vars(row, language))
        raw = await _gen(model, prompt)
        title, body = _parse_generated(raw)
        if kind == "gen_per_row_original":
            # обновляем тело оригинала + сброс spin_formula (переспиннится на Start)
            async with WriteSession() as s:
                await s.execute(update(Text).where(Text.id == group_text_id).values(
                    body=body, title=title, spin_formula=None, content_hash=_hash(body)))
                await s.execute(update(TextItem).where(TextItem.id == item_id).values(
                    text_id=group_text_id, title=title, content_hash=_hash(body),
                    byte_size=len(body.encode()), status=TextItemStatus.PENDING.value,
                    last_error=None))
                await s.commit()
        else:  # per_post
            await _set_item_text(item_id, body, title=title, source="generated",
                                 gen_model=model.model_id)
        return {"ok": True, "status": "generated", "kind": kind}
    except GenerationError as e:
        await _item_gen_failed(item_id, f"generation failed: {e}")
        return {"ok": False, "status": "gen_error", "error": str(e)}


# Drip-стриминг: на каждом заходе генерим айтемы, «созревающие» в ближайшие N
# часов (не весь файл). Постинг сам перевзведёт run в scheduled до след. порции.
_STREAM_GEN_HORIZON = timedelta(hours=6)


async def apply_drip_not_before(run_id: int, spread_days: int,
                                scheduled_for=None) -> None:
    """Размазать not_before айтемов рана по ДНЯМ окна spread_days: каждый айтем →
    полночь СЛУЧАЙНОГО дня из [день старта .. +spread_days). Тогда весь дневной
    пул «созревает» разом в его полночь → постинг гонит дневную пачку и паркуется
    до следующего дня (без поминутного churn'а, который душил параллельную
    генерацию). Просроченные дни (not_before в прошлом — старт-день или
    пропущенные из-за сбоя/зависания) всегда «созревшие» → догоняются, не теряются.
    Идемпотентно — трогает только айтемы без not_before. Окно стартует от
    scheduled_for (если в будущем) либо now. spread_days<=0 → no-op."""
    if not spread_days or spread_days <= 0:
        return
    now_ts = datetime.now(UTC)
    ws = scheduled_for if (scheduled_for and scheduled_for > now_ts) else now_ts
    async with WriteSession() as s:
        await s.execute(sql("""
            UPDATE text_items SET not_before = date_trunc('day', (:ws)::timestamptz)
                + (floor(random() * :days) * interval '1 day')
            WHERE posting_run_id = :rid AND not_before IS NULL
        """), {"rid": run_id, "ws": ws, "days": spread_days})
        await s.commit()


async def create_empty_campaign_items(
    run_id: int, project_id: int, rows: list[dict], mode: str, language: str | None = None,
) -> tuple[int, list[dict], list[int]]:
    """Manual «не генерим сразу»: создаём ПУСТЫЕ айтемы (видны в таблице сразу,
    без текста), наполняются по «Сгенерировать тексты»/пер-айтем. Для gen_per_row —
    плейсхолдер-оригинал (пустое тело) + группа (оригинал+спины, text_id=NULL).
    Возвращает (total_items, groups, main_text_ids)."""
    if mode == "gen_per_row":
        groups: list[dict] = []
        main_ids: list[int] = []
        planned = 0
        for i, row in enumerate(rows):
            count = max(1, int(row.get("count") or 1))
            link, anchor = row.get("link"), (row.get("anchor") or "")
            async with WriteSession() as s:
                orig = Text(body="", spin_formula=None, title=None,
                            lang=(row.get("language") or language), source="generated",
                            reusable=True, content_hash=_hash(f"empty-orig-{run_id}-{i}"))
                s.add(orig)
                await s.flush()
                oid = orig.id
                grp = await _create_group_items(s, run_id, project_id, orig, link,
                                                anchor, count, row=row, empty=True)
                await s.commit()
            main_ids.append(oid)
            groups.append({"text_id": oid, "link": link, "anchor": anchor,
                           "count": count, **grp})
            planned += count
        return planned, groups, main_ids

    # gen_per_post: count независимых пустых айтемов на строку
    total = 0
    item_rows: list[dict] = []
    for row in rows:
        count = max(1, int(row.get("count") or 1))
        td = normalize_domain(row.get("link") or "")
        for _ in range(count):
            item_rows.append({
                "posting_run_id": run_id, "project_id": project_id, "text_id": None,
                "original_filename": "(пусто)", "title": None,
                "content_hash": hashlib.sha256(f"empty-{run_id}-{total}".encode()).hexdigest(),
                "byte_size": 0, "status": TextItemStatus.PENDING.value,
                "link_url": row.get("link"), "link_anchor": (row.get("anchor") or None),
                "target_domain": td, "lang": (row.get("language") or language),
                "gen_row": row,
            })
            total += 1
    if item_rows:
        async with WriteSession() as s:
            await s.execute(TextItem.__table__.insert(), item_rows)
            await s.commit()
    return total, [], []


async def _gen_group_original(g: dict, gp: dict) -> bool:
    """Сгенерировать AI-тело оригинала группы в плейсхолдер-Text. НЕ трогаем
    айтемы (их финализирует _fanout_one_group), чтобы стриминг-постинг не схватил
    полу-готовый текст. True если сгенерили."""
    async with WriteSession() as s:
        item = await s.scalar(select(TextItem).where(TextItem.id == g["original_item_id"]))
        row = (item.gen_row if item else None) or {"link": g.get("link"), "anchor": g.get("anchor")}
        model = await pick_model(s, purpose="content", model_pk=gp.get("ai_model_id"))
        tpl = (await s.scalar(select(PromptTemplate).where(
            PromptTemplate.id == gp.get("prompt_template_id")))
            if gp.get("prompt_template_id") else None)
    if model is None:
        return False
    prompt = render_prompt(tpl.body if tpl else "{keyword}", _row_vars(row, gp.get("language")))
    try:
        raw = await _gen(model, prompt)
    except GenerationError:
        return False
    title, body = _parse_generated(raw)
    async with WriteSession() as s:
        await s.execute(update(Text).where(Text.id == g["text_id"]).values(
            body=body, title=title, spin_formula=None, content_hash=_hash(body)))
        await s.commit()
    return True


async def generate_run_items(run_id: int, *, finalize: bool = False) -> dict:
    """Наполнить предсозданные пустые айтемы.
    - finalize=False (manual bulk): gen_per_row → только ОРИГИНАЛЫ (спины на Start),
      gen_per_post → все. По завершении ран → READY.
    - finalize=True (auto-стриминг): gen_per_row → оригинал + СРАЗУ fanout группы
      (финальные постабельные айтемы), gen_per_post → все. Статус не трогаем —
      им управляет параллельный постинг.
    На pause/cancel — останавливаемся. Прогресс в gen_params (красный бар)."""
    async with WriteSession() as s:
        run = await s.scalar(select(PostingRun).where(PostingRun.id == run_id))
        if run is None:
            return {"ok": False, "error": "not found"}
        mode = run.content_mode
        gp = run.gen_params or {}
        groups = gp.get("fanout_groups") or []

    async def _stop() -> bool:
        async with WriteSession() as s:
            r = (await s.execute(select(
                PostingRun.pause_requested, PostingRun.cancel_requested)
                .where(PostingRun.id == run_id))).first()
        return bool(r and (r[0] or r[1]))

    # gen_active=true → стрим-постинг (manual gen_per_post: «Старт постинга» поверх
    # идущей генерации) ждёт новые готовые тексты, пока генерация не закончится.
    await set_gen_active(run_id, True)
    try:
        # ── gen_per_row + finalize: оригинал + fanout per group (стриминг) ──
        if mode == "gen_per_row" and finalize:
            async with WriteSession() as s:
                spin_model = await pick_model(s, purpose="spin")
            horizon = datetime.now(UTC) + _STREAM_GEN_HORIZON
            async with WriteSession() as s:
                # ORDER BY not_before ASC: сначала самые «просроченные» (бэклог
                # прошлых дней), потом ближайшие. Иначе генерация гонит будущее, а
                # вчерашний недобор висит несгенерённым и постить нечего.
                ordered_origs = (await s.execute(select(TextItem.id).where(
                    TextItem.posting_run_id == run_id, TextItem.text_id.is_(None),
                    TextItem.status == TextItemStatus.PENDING.value,
                    or_(TextItem.not_before.is_(None),  # drip: только «созревшие» оригиналы
                        TextItem.not_before <= horizon))
                    .order_by(TextItem.not_before.asc().nulls_first()))).scalars().all()
            _g_by_orig = {g.get("original_item_id"): g for g in groups}
            todo = [_g_by_orig[oid] for oid in ordered_origs if oid in _g_by_orig]
            await _gen_progress(run_id, done=0, total=len(todo))
            # Параллельно: до content_gen_concurrency ГРУПП одновременно. Каждая
            # группа независима (свой оригинал+fanout, свои сессии, свои text/items),
            # а gen_limiter держит глобальный потолок LLM-вызовов. Раньше здесь был
            # последовательный `for` → эффективная конкуренция ≈1 (gen_slots=1) и весь
            # drip-стрим упирался в ~1 вызов за раз (узкое место темпа). Порядок
            # not_before ASC (просрочка первой) сохраняется: gather стартует корутины
            # в порядке todo, sem держит окно ширины conc.
            conc = await _gen_concurrency()
            sem = asyncio.Semaphore(conc)
            done = 0
            stopped = False

            async def _gen_group(g: dict) -> None:
                nonlocal done, stopped
                async with sem:
                    if stopped:
                        return
                    try:
                        if await _gen_group_original(g, gp):
                            async with WriteSession() as s:  # атомарно финализируем группу
                                await _fanout_one_group(s, g, spin_model)
                                await s.commit()
                    except Exception as e:  # одна группа не валит весь стрим
                        log.warning("content_engine.stream_gen.group_failed",
                                    run_id=run_id, error=str(e))
                    done += 1
                    if done % conc == 0 or done >= len(todo):
                        await _gen_progress(run_id, done=done)
                        if await _stop():
                            stopped = True

            await asyncio.gather(*(_gen_group(g) for g in todo))
            await _gen_progress(run_id, done=done)
            log.info("content_engine.stream_gen.done", run_id=run_id, groups=done)
            return {"ok": True, "generated": done}

        # ── manual / gen_per_post: пер-айтем generate_item ──
        async with WriteSession() as s:
            conds = [TextItem.posting_run_id == run_id, TextItem.text_id.is_(None),
                     TextItem.status == TextItemStatus.PENDING.value]
            if finalize:  # стриминг (auto): только «созревшие» в горизонте (drip-генерация)
                horizon = datetime.now(UTC) + _STREAM_GEN_HORIZON
                conds.append(or_(TextItem.not_before.is_(None), TextItem.not_before <= horizon))
            # ORDER BY not_before ASC — просроченный бэклог генерим первым.
            empty_ids_ordered = list((await s.execute(select(TextItem.id).where(*conds)
                .order_by(TextItem.not_before.asc().nulls_first()))).scalars().all())
            empty_ids = set(empty_ids_ordered)
        if mode == "gen_per_row":
            _origs = {g["original_item_id"] for g in groups}
            ordered = [iid for iid in empty_ids_ordered if iid in _origs]
        else:
            ordered = empty_ids_ordered
        await _gen_progress(run_id, done=0, total=len(ordered))
        # Параллельно: до content_gen_concurrency айтемов одновременно (generate_item
        # независим — своя сессия/claim). Глобальный потолок LLM держит gen_limiter,
        # так что фактическая конкуренция вызовов не превысит настройку и при многих
        # прогонах. Периодически проверяем стоп (пауза/отмена), не на каждом айтеме.
        conc = await _gen_concurrency()
        sem = asyncio.Semaphore(conc)
        done = 0
        stopped = False

        async def _gen_one(iid: int) -> None:
            nonlocal done, stopped
            async with sem:
                if stopped:
                    return
                try:
                    # per-item таймаут: зависший AI-вызов отменяем, айтем остаётся
                    # несгенерированным (text_id NULL) → перегенерится позже, а не
                    # вешает весь gather → всю генерацию.
                    await asyncio.wait_for(
                        generate_item(iid, regenerate=False),
                        timeout=GEN_ITEM_TIMEOUT_S)
                except Exception as e:  # один айтем не валит весь bulk (вкл. TimeoutError)
                    log.warning("content_engine.bulk_gen.item_failed",
                                item_id=iid, error=str(e))
                done += 1
                if done % conc == 0 or done >= len(ordered):
                    await _gen_progress(run_id, done=done)
                    if await _stop():
                        stopped = True

        await asyncio.gather(*(_gen_one(iid) for iid in ordered))
        await _gen_progress(run_id, done=done)
        # manual bulk → обратно READY (виден Start). НО только если ран всё ещё
        # UNPACKING: если постинг уже подхватил ран (RUNNING/QUEUED — параллельный
        # gen+post), статус его, не перетираем. Стриминг (finalize) не трогает.
        if not finalize:
            async with WriteSession() as s:
                await s.execute(update(PostingRun).where(
                    PostingRun.id == run_id,
                    PostingRun.status == PostingRunStatus.UNPACKING.value)
                    .values(status=PostingRunStatus.READY.value))
                await s.commit()
        log.info("content_engine.bulk_gen.done", run_id=run_id, generated=done)
        return {"ok": True, "generated": done}
    finally:
        await set_gen_active(run_id, False)


async def _fail_run(run_id: int, msg: str) -> None:
    async with WriteSession() as s:
        gp = dict((await s.scalar(
            select(PostingRun.gen_params).where(PostingRun.id == run_id))) or {})
        gp["error"] = msg[:500]  # причина видна в UI (шапка рана)
        await s.execute(update(PostingRun).where(PostingRun.id == run_id).values(
            status=PostingRunStatus.FAILED.value, finished_at=datetime.now(UTC), gen_params=gp))
        await s.commit()
    log.warning("content_engine.campaign.failed", run_id=run_id, error=msg)
