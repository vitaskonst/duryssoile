"""Conditional GET: ETag and If-None-Match.

Lets clients keep a copy of a word or a clip (the mobile app's offline
favourites) and cheaply ask whether it changed: a request whose
If-None-Match names the current ETag gets 304 Not Modified with no body.
Requests without the header get the usual full response.
"""

import hashlib

from fastapi import Request, Response


def etag_of(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def is_fresh(request: Request, etag: str) -> bool:
    """Whether the client's If-None-Match already names this ETag."""
    header = request.headers.get('if-none-match')
    if not header:
        return False
    if header.strip() == '*':
        return True
    # Weak comparison, as RFC 9110 prescribes for If-None-Match.
    candidates = {tag.strip().removeprefix('W/') for tag in header.split(',')}
    return etag.removeprefix('W/') in candidates


def not_modified(etag: str, headers: dict[str, str] | None = None) -> Response:
    return Response(status_code=304, headers={'ETag': etag, **(headers or {})})
