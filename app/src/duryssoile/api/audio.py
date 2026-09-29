import enum
import io
import logging
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage, voice
from ..db import get_session
from ..models import Word

router = APIRouter()
logger = logging.getLogger('duryssoile')


class AudioFormat(str, enum.Enum):
    opus = 'opus'


async def opus_version(word: Word) -> bytes:
    """The word's clip as OGG/Opus: from storage, or converted and stored."""
    key = storage.opus_key(word.id)
    try:
        body, _, _ = await storage.get_object(key)
        return body
    except storage.AudioNotFound:
        pass

    source, _, _ = await storage.get_object(word.audio_key)
    try:
        body = await voice.to_opus(source)
    except voice.UnsupportedClip:
        # Only clips uploaded before uploads were restricted to convertible
        # formats can get here.
        raise HTTPException(status_code=415, detail='No voice version of this clip') from None
    except voice.ConversionFailed:
        logger.exception('could not convert the clip of word %s', word.id)
        raise HTTPException(status_code=500, detail='Could not convert the clip') from None
    await storage.put_object(key, io.BytesIO(body), 'audio/ogg')
    return body


@router.get('/{word_id}')
async def read_audio(
    word_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    format: Annotated[
        AudioFormat | None,
        Query(description='opus: the clip re-encoded as a mono OGG/Opus voice note.'),
    ] = None,
) -> Response:
    """Stream a word's pronunciation out of RustFS.

    The bytes are proxied rather than served as a presigned redirect: a
    presigned URL would point at the RustFS endpoint, which is not reachable
    from outside the compose network. Without `format` the response is the
    clip as uploaded, unchanged from the 2023 API.
    """
    word = await session.get(Word, word_id)

    if word is None or not word.audio_key:
        raise HTTPException(status_code=404, detail='Not Found')

    try:
        if format is AudioFormat.opus:
            body = await opus_version(word)
            content_type, size = 'audio/ogg', len(body)
            filename = f'{word.id}.ogg'
        else:
            body, content_type, size = await storage.get_object(word.audio_key)
            filename = word.audio_key.rsplit('/', 1)[-1]
    except storage.AudioNotFound:
        raise HTTPException(status_code=404, detail='Not Found') from None
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
