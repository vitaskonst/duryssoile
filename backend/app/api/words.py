from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_session
from ..models import Word, WordType
from .schemas import SortingOrder, WordTypeQuery

router = APIRouter()


def serialize(word: Word) -> dict[str, Any]:
    """Match the original API response byte-for-byte.

    The v1 service returned the raw word dict minus 'filename', which meant
    commonly-mispronounced words carried NO 'correctVersions' key at all
    (their JSON never had one). The Telegram bot relies on this, so we omit
    the key when there are no correct versions rather than sending [].
    """
    payload: dict[str, Any] = {'id': word.id, 'word': word.word}

    if word.correct_versions:
        payload['correctVersions'] = [
            # Same rule: usage examples are omitted, not null, when absent.
            {
                key: value
                for key, value in (
                    ('word', version.word),
                    ('incorrectUsage', version.incorrect_usage),
                    ('correctUsage', version.correct_usage),
                )
                if value is not None
            }
            for version in word.correct_versions
        ]

    payload['type'] = word.type.value
    return payload


@router.get('')
async def read_words(
    type: Annotated[WordTypeQuery, Query(description='Word type to return.')],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    filter: Annotated[
        str, Query(description='Case-insensitive prefix every word must start with.')
    ] = '',
    offset: Annotated[
        int, Query(ge=0, description='PAGE NUMBER, starting at 0 (not a row offset).')
    ] = 0,
    limit: Annotated[int, Query(ge=0, description='Page size.')] = 20,
    sort: Annotated[SortingOrder, Query(description='Sort by id.')] = SortingOrder.ascending,
) -> list[dict[str, Any]]:
    if limit > settings.max_page_size:
        raise HTTPException(
            status_code=400,
            detail=[
                {
                    'loc': ['query', 'limit'],
                    'msg': f'value must not exceed {settings.max_page_size}',
                    'type': 'value_error',
                }
            ],
        )

    statement = select(Word).where(Word.type == WordType(type.value))

    if filter:
        # lower() matches the functional index in db/schema.sql; escape the
        # LIKE metacharacters so a filter of '100%' cannot match everything.
        escaped = filter.lower().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        statement = statement.where(Word.word.ilike(f'{escaped}%', escape='\\'))

    statement = statement.order_by(
        Word.id.desc() if sort is SortingOrder.descending else Word.id.asc()
    )

    # NOTE: the original computed result_ids[offset*limit:(offset+1)*limit],
    # i.e. `offset` is a page index. The Telegram bot increments it by 1 per
    # page, so this semantic is load-bearing -- do not "fix" it to a row offset.
    statement = statement.offset(offset * limit).limit(limit)

    words = (await session.scalars(statement)).all()
    return [serialize(word) for word in words]


@router.get('/{word_id}')
async def read_word(
    word_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    word = await session.get(Word, word_id)

    if word is None:
        raise HTTPException(status_code=404, detail='Not Found')

    return serialize(word)
