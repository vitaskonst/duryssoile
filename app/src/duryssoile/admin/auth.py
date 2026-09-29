"""Single-password admin auth.

There is one operator, so there is no user table: the password comes from
ADMIN_PASSWORD in the environment and the session is a signed cookie
(Starlette's SessionMiddleware, keyed by SECRET_KEY).
"""

import secrets
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from ..config import Settings, get_settings

SESSION_KEY = 'admin'

# Brute-force throttle. The app is single-process per worker, so this is a
# best-effort speed bump, not a distributed rate limiter.
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60
_attempts: dict[str, tuple[int, float]] = {}


def _client_key(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def throttle_remaining(request: Request) -> int:
    """Seconds the caller must wait, or 0 if they may try now."""
    count, first_seen = _attempts.get(_client_key(request), (0, 0.0))

    if count < _MAX_ATTEMPTS:
        return 0

    elapsed = time.monotonic() - first_seen
    if elapsed >= _LOCKOUT_SECONDS:
        _attempts.pop(_client_key(request), None)
        return 0

    return int(_LOCKOUT_SECONDS - elapsed) + 1


def record_failure(request: Request) -> None:
    key = _client_key(request)
    count, first_seen = _attempts.get(key, (0, time.monotonic()))
    _attempts[key] = (count + 1, first_seen)


def verify_password(candidate: str, settings: Settings) -> bool:
    # Constant-time comparison so the response time leaks nothing.
    return secrets.compare_digest(candidate, settings.admin_password)


def log_in(request: Request) -> None:
    _attempts.pop(_client_key(request), None)
    request.session[SESSION_KEY] = True


def log_out(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


class RedirectToLogin(HTTPException):
    """Raised instead of returning 401 so browsers land on the login form."""

    def __init__(self, next_url: str) -> None:
        super().__init__(status_code=status.HTTP_303_SEE_OTHER)
        self.next_url = next_url


def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not is_logged_in(request):
        raise RedirectToLogin(request.url.path)


def redirect_to_login_handler(request: Request, exc: RedirectToLogin):
    # Relative Location on purpose: an absolute url_for() depends on the Host
    # header, which a reverse proxy can strip the port from.
    target = request.url_for('admin_login_form').include_query_params(
        next=exc.next_url
    )
    relative = target.path
    if target.query:
        relative = f'{relative}?{target.query}'

    return RedirectResponse(relative, status_code=status.HTTP_303_SEE_OTHER)
