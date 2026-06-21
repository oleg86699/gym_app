"""Генерация правдоподобной личности для нового WP-пользователя.

Логин вида реального имени (marie.dubois23), email на домене сайта,
крепкий пароль. Цель — чтобы созданный нами аккаунт не выделялся в списке
пользователей сайта как явно служебный/бот.
"""
from __future__ import annotations

import secrets
import string

# Небольшие интернациональные пулы — достаточно для разнообразия логинов.
_FIRST = (
    "marie", "lucas", "sophie", "david", "anna", "paul", "laura", "tom",
    "elena", "mark", "julia", "peter", "nina", "alex", "clara", "leon",
    "sara", "max", "emma", "jan", "lena", "noah", "mia", "felix",
    "diana", "victor", "rosa", "hugo", "ines", "milan",
)
_LAST = (
    "dubois", "weber", "rossi", "novak", "smith", "garcia", "muller",
    "lopez", "kowalski", "ferrari", "santos", "meyer", "bauer", "costa",
    "horvat", "jensen", "klein", "moreau", "ricci", "fischer", "vidal",
    "nguyen", "haas", "lang", "bruno", "marin", "petit", "roy", "simon",
    "vega",
)
_SEPS = (".", "_", "")
_SPECIAL = "!@#$%^&*-_=+"


def _secure_choice(seq):
    return seq[secrets.randbelow(len(seq))]


def generate_username() -> str:
    first = _secure_choice(_FIRST)
    last = _secure_choice(_LAST)
    sep = _secure_choice(_SEPS)
    suffix = "" if secrets.randbelow(2) else str(secrets.randbelow(90) + 10)
    uname = f"{first}{sep}{last}{suffix}"
    return uname[:60]  # WP user_login limit 60


def generate_password(length: int = 20) -> str:
    """Крепкий пароль: буквы+цифры+спецсимвол, гарантированно проходит
    WP-проверку силы пароля."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(_secure_choice(alphabet) for _ in range(length - 3))
    # гарантируем по одному из классов
    extra = (
        _secure_choice(string.ascii_uppercase)
        + _secure_choice(string.digits)
        + _secure_choice(_SPECIAL)
    )
    return body + extra


def generate_identity(domain: str) -> tuple[str, str, str]:
    """(username, email, password). Email — на домене самого сайта."""
    username = generate_username()
    # email-локалпарт без точки в конце/спецов — берём username как есть
    email = f"{username.replace('_', '.').strip('.')}@{domain}"
    password = generate_password()
    return username, email, password
