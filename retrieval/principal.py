"""Principal context — the KB owner for the current call chain.

A module-level :class:`contextvars.ContextVar` carries the Telegram user id
from the handler boundary down to the kb tools; tools pass it on as an
explicit argument (contextvars do not cross the retrieval executor).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token

log = logging.getLogger("vektor.retrieval.principal")

_user: ContextVar[str] = ContextVar("vektor.user", default="0")


def set_user(user_id: str) -> Token[str]:
    """Set the current principal; return a token for :func:`reset_user`."""
    return _user.set(user_id)


def get_user() -> str:
    """Return the current principal (``"0"`` when none was set)."""
    return _user.get()


def reset_user(token: Token[str]) -> None:
    """Restore the principal to its state before the matching :func:`set_user`."""
    _user.reset(token)
