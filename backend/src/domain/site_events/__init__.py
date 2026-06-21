"""Site error event logging — append-only история ошибок по сайтам."""

from .service import list_site_events, record_site_event

__all__ = ["record_site_event", "list_site_events"]
