from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..db import get_session
from ..models import Word

router = APIRouter()


@router.get('/{word_id}')
async def read_audio(
    word_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Stream a word's pronunciation out of RustFS.

    The bytes are proxied rather than served as a presigned redirect: a
    presigned URL would point at the RustFS endpoint, which is not reachable
    from outside the compose network.
    """
    word = await session.get(Word, word_id)

    if word is None or not word.audio_key:
        raise HTTPException(status_code=404, detail='Not Found')

    try:
        body, content_type, size = await storage.get_object(word.audio_key)
    except storage.AudioNotFound:
        raise HTTPException(status_code=404, detail='Not Found') from None

    filename = word.audio_key.rsplit('/', 1)[-1]
    return Response(
        content=body,
        media_type=content_type,
        headers={
            'Content-Length': str(size),
            # RFC 5987 encoding -- the filenames are Cyrillic.
            'Content-Disposition': f"inline; filename*=UTF-8''{quote(filename)}",
            'Cache-Control': 'public, max-age=86400',
        },
    )
