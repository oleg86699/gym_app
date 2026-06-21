"""AI-генерация (C2): шаблонизатор промптов + вызов провайдера (httpx REST).

Без новых зависимостей — REST-вызовы openai/anthropic/google через httpx.
Ключ провайдера хранится шифрованным (core.crypto). На любой сбой — GenerationError
(воркер ловит, оставляет задачу на повтор).
"""

from __future__ import annotations

import re

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.crypto import decrypt_password
from infrastructure.db.models import AiModel, AiProvider

log = structlog.get_logger(__name__)

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class GenerationError(Exception):
    pass


def render_prompt(body: str, variables: dict) -> str:
    """Подставить {переменные} из variables. Неизвестные плейсхолдеры оставляем
    как есть (не падаем). Как _safe_format в gym_gen_content_casino_new."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(variables.get(key, m.group(0)))
    return _VAR_RE.sub(repl, body or "")


async def pick_model(session: AsyncSession, *, purpose: str,
                     model_pk: int | None = None) -> AiModel | None:
    """Выбрать активную модель: по id (если задан) либо первую активную с нужным
    purpose (content/spin), иначе любую активную (any)."""
    if model_pk:
        m = await session.scalar(select(AiModel).where(AiModel.id == model_pk))
        if m and m.is_active:
            return m
    stmt = (select(AiModel).join(AiProvider, AiModel.provider_id == AiProvider.id)
            .where(AiModel.is_active.is_(True), AiProvider.is_active.is_(True),
                   AiModel.purpose.in_([purpose, "any"]))
            .order_by(AiModel.id).limit(1))
    return await session.scalar(stmt)


async def generate_text(session: AsyncSession, *, model: AiModel, prompt: str,
                        timeout: float = 120.0) -> str:
    """Сгенерировать текст моделью. Возвращает текст или кидает GenerationError."""
    provider = await session.scalar(
        select(AiProvider).where(AiProvider.id == model.provider_id))
    if provider is None or not provider.is_active:
        raise GenerationError("provider inactive/missing")
    try:
        api_key = decrypt_password(provider.api_key_enc)
    except Exception as e:
        raise GenerationError(f"bad api key: {e}") from e
    if not api_key:
        raise GenerationError("empty api key")

    ptype = provider.type
    try:
        if ptype == "openai":
            return await _openai(provider, model, prompt, api_key, timeout)
        if ptype == "anthropic":
            return await _anthropic(provider, model, prompt, api_key, timeout)
        if ptype == "google":
            return await _google(provider, model, prompt, api_key, timeout)
        raise GenerationError(f"unknown provider type {ptype}")
    except GenerationError:
        raise
    except (httpx.TimeoutException, httpx.NetworkError) as e:
        raise GenerationError(f"network: {e}") from e
    except Exception as e:
        raise GenerationError(f"call failed: {str(e)[:300]}") from e


async def _openai(p: AiProvider, m: AiModel, prompt: str, key: str, timeout: float) -> str:
    base = (p.base_url or "https://api.openai.com/v1").rstrip("/")
    # Новые модели (gpt-5.x / o-series) сменили параметры: max_tokens →
    # max_completion_tokens, и часть не принимает кастомный temperature.
    # Шлём современный набор и авто-чиним по тексту 400-ответа.
    payload: dict = {
        "model": m.model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": m.temperature,
        "max_completion_tokens": m.max_tokens,
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        for _ in range(4):
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"}, json=payload)
            if r.status_code == 200:
                data = r.json()
                txt = (data.get("choices") or [{}])[0].get("message", {}).get("content")
                if not txt:
                    raise GenerationError("openai empty content")
                return txt
            err = r.text.lower()
            # авто-фолбэк несовместимых параметров
            if r.status_code == 400 and "temperature" in err and "temperature" in payload:
                payload.pop("temperature"); continue
            if r.status_code == 400 and "max_completion_tokens" in err and "max_tokens" not in payload:
                payload["max_tokens"] = payload.pop("max_completion_tokens"); continue
            if r.status_code == 400 and "'max_tokens'" in err and "max_completion_tokens" not in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens"); continue
            raise GenerationError(f"openai {r.status_code}: {r.text[:300]}")
        raise GenerationError("openai: param negotiation failed")


async def _anthropic(p: AiProvider, m: AiModel, prompt: str, key: str, timeout: float) -> str:
    base = (p.base_url or "https://api.anthropic.com").rstrip("/")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{base}/v1/messages",
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                         json={"model": m.model_id, "max_tokens": m.max_tokens,
                               "temperature": m.temperature,
                               "messages": [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            raise GenerationError(f"anthropic {r.status_code}: {r.text[:200]}")
        data = r.json()
        parts = data.get("content") or []
        txt = parts[0].get("text") if parts else None
        if not txt:
            raise GenerationError("anthropic empty content")
        return txt


async def _google(p: AiProvider, m: AiModel, prompt: str, key: str, timeout: float) -> str:
    base = (p.base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base}/v1beta/models/{m.model_id}:generateContent?key={key}"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": m.temperature,
                                 "maxOutputTokens": m.max_tokens}})
        if r.status_code != 200:
            raise GenerationError(f"google {r.status_code}: {r.text[:200]}")
        data = r.json()
        cands = data.get("candidates") or []
        parts = (cands[0].get("content", {}).get("parts") if cands else None) or []
        txt = parts[0].get("text") if parts else None
        if not txt:
            raise GenerationError("google empty content")
        return txt
