import io
import math
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..config import Settings, get_settings
from ..db import get_session
from ..models import CorrectVersion, Word, WordType
from . import auth, labels

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

# Every template gets the Kazakh type labels and the per-type navigation.
templates.env.globals['TYPE_LABELS'] = labels.TYPE_LABELS
templates.env.globals['WORD_TYPES'] = list(WordType)
templates.env.globals['supports_correct_versions'] = labels.supports_correct_versions

router = APIRouter()

PAGE_SIZE = 25
ALLOWED_AUDIO_TYPES = {
    'audio/mpeg': '.mp3',
    'audio/mp3': '.mp3',
    'audio/wav': '.wav',
    'audio/x-wav': '.wav',
    'audio/ogg': '.ogg',
    'audio/opus': '.opus',
    'audio/mp4': '.m4a',
    'audio/aac': '.aac',
}
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def relative_url_for(request: Request, name: str, **params) -> str:
    """Build a path-only URL.

    url_for() returns an absolute URL derived from the Host header; a reverse
    proxy that rewrites or drops the port then sends admins to the wrong
    origin. A relative Location avoids the whole question.
    """
    url = request.url_for(name, **params)
    return f'{url.path}?{url.query}' if url.query else url.path


def escape_like(text: str) -> str:
    """Escape LIKE metacharacters so a search for '%' matches a literal '%'."""
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


# --------------------------------------------------------------------------
# auth
#
# Declared before the /{word_type} routes below: FastAPI matches in
# declaration order, so these literal paths must win over the type parameter.
# --------------------------------------------------------------------------

@router.get('/login', response_class=HTMLResponse, name='admin_login_form')
async def login_form(request: Request, next: str = '/admin/'):
    if auth.is_logged_in(request):
        return RedirectResponse(next, status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request, 'login.html', {'next': next, 'error': None}
    )


@router.post('/login', name='admin_login')
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = '/admin/',
):
    wait = auth.throttle_remaining(request)
    if wait:
        return templates.TemplateResponse(
            request,
            'login.html',
            {
                'next': next,
                'error': f'Тым көп әрекет жасалды. {wait} секундтан кейін '
                         f'қайталап көріңіз.',
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not auth.verify_password(password, settings):
        auth.record_failure(request)
        return templates.TemplateResponse(
            request,
            'login.html',
            {'next': next, 'error': 'Құпиясөз қате.'},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    auth.log_in(request)
    # Only allow same-site relative redirects, so ?next= cannot be used to
    # bounce an admin off to another host after login.
    target = next if next.startswith('/') and not next.startswith('//') else '/admin/'
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.get('/logout', name='admin_logout')
async def logout(request: Request):
    auth.log_out(request)
    return RedirectResponse(
        relative_url_for(request, 'admin_login_form'),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# --------------------------------------------------------------------------
# create / edit / delete, addressed by word id
# --------------------------------------------------------------------------

def parse_correct_versions(form, word_type: WordType) -> list[CorrectVersion]:
    """Rebuild the correct-version rows from the repeated form fields.

    Returns nothing for a type that does not carry correct versions, so a
    hidden editor cannot smuggle rows onto a commonly-mispronounced word.
    """
    if not labels.supports_correct_versions(word_type):
        return []

    words = form.getlist('cv_word')
    incorrect = form.getlist('cv_incorrect_usage')
    correct = form.getlist('cv_correct_usage')

    versions: list[CorrectVersion] = []
    for position, text in enumerate(words):
        text = text.strip()
        if not text:
            continue

        def at(values, index=position):
            value = values[index].strip() if index < len(values) else ''
            return value or None

        versions.append(
            CorrectVersion(
                word=text,
                incorrect_usage=at(incorrect),
                correct_usage=at(correct),
                position=len(versions),
            )
        )

    return versions


def parse_word_type(raw: str) -> WordType:
    """Validate a type submitted in a form."""
    try:
        return WordType(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail='Сөз түрі белгісіз') from None


def word_type_from_path(word_type: str) -> WordType:
    """Validate a type taken from the URL.

    Parsed by hand rather than declared as an Enum path parameter: an unknown
    slug is a mistyped admin URL, which deserves a 404 rather than the 422 a
    parameter validation error would produce.
    """
    try:
        return WordType(word_type)
    except ValueError:
        raise HTTPException(status_code=404, detail='Сөз түрі табылмады') from None


async def store_audio(word: Word, upload: UploadFile) -> None:
    content_type = (upload.content_type or '').split(';')[0].strip().lower()
    suffix = ALLOWED_AUDIO_TYPES.get(content_type)

    if suffix is None:
        raise HTTPException(
            status_code=400,
            detail=f'Дыбыс форматы қолданылмайды: {content_type or "белгісіз"}',
        )

    body = await upload.read()
    if len(body) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413, detail='Дыбыс файлы тым үлкен (ең көбі 10 МБ)'
        )
    if not body:
        raise HTTPException(status_code=400, detail='Дыбыс файлы бос')

    key = f'{word.type.value}/{word.id}{suffix}'
    await storage.put_object(key, io.BytesIO(body), content_type)

    # Drop the previous object if the key changed (e.g. mp3 -> wav).
    if word.audio_key and word.audio_key != key:
        try:
            await storage.delete_object(word.audio_key)
        except Exception:  # noqa: BLE001 - a stale object is not fatal
            pass

    word.audio_key = key


@router.post(
    '/words',
    name='admin_create_word',
    dependencies=[Depends(auth.require_admin)],
)
async def create_word(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    word: Annotated[str, Form()],
    type: Annotated[str, Form()],
    audio: Annotated[UploadFile | None, File()] = None,
):
    form = await request.form()
    word_type = parse_word_type(type)

    if not word.strip():
        raise HTTPException(status_code=400, detail='Сөз бос болмауы керек')

    row = Word(word=word.strip(), type=word_type)
    row.correct_versions = parse_correct_versions(form, word_type)

    session.add(row)
    # Flush to allocate the id, which the audio object key embeds.
    await session.flush()

    if audio is not None and audio.filename:
        await store_audio(row, audio)

    await session.commit()

    return RedirectResponse(
        relative_url_for(request, 'admin_edit_word', word_id=row.id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    '/words/{word_id}',
    response_class=HTMLResponse,
    name='admin_edit_word',
    dependencies=[Depends(auth.require_admin)],
)
async def edit_word(
    request: Request,
    word_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    row = await session.get(Word, word_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Сөз табылмады')

    return templates.TemplateResponse(
        request,
        'edit.html',
        {'word': row, 'word_type': row.type, 'error': None},
    )


@router.post(
    '/words/{word_id}',
    name='admin_update_word',
    dependencies=[Depends(auth.require_admin)],
)
async def update_word(
    request: Request,
    word_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    word: Annotated[str, Form()],
    type: Annotated[str, Form()],
    audio: Annotated[UploadFile | None, File()] = None,
    delete_audio: Annotated[str, Form()] = '',
):
    form = await request.form()

    row = await session.get(Word, word_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Сөз табылмады')

    word_type = parse_word_type(type)
    if not word.strip():
        raise HTTPException(status_code=400, detail='Сөз бос болмауы керек')

    row.word = word.strip()
    row.type = word_type
    # Replace wholesale: the delete-orphan cascade removes the old rows.
    row.correct_versions = parse_correct_versions(form, word_type)

    if delete_audio and row.audio_key:
        try:
            await storage.delete_object(row.audio_key)
        except Exception:  # noqa: BLE001
            pass
        row.audio_key = None

    if audio is not None and audio.filename:
        await store_audio(row, audio)

    await session.commit()

    return RedirectResponse(
        relative_url_for(request, 'admin_edit_word', word_id=row.id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post(
    '/words/{word_id}/delete',
    name='admin_delete_word',
    dependencies=[Depends(auth.require_admin)],
)
async def delete_word(
    request: Request,
    word_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    row = await session.get(Word, word_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Сөз табылмады')

    word_type = row.type

    if row.audio_key:
        try:
            await storage.delete_object(row.audio_key)
        except Exception:  # noqa: BLE001
            pass

    await session.delete(row)
    await session.commit()

    # Back to the list the word came from.
    return RedirectResponse(
        relative_url_for(request, 'admin_list', word_type=word_type.value),
        status_code=status.HTTP_303_SEE_OTHER,
    )


# --------------------------------------------------------------------------
# one list page per word type
# --------------------------------------------------------------------------

@router.get('/', name='admin_index', dependencies=[Depends(auth.require_admin)])
async def index(request: Request):
    """There is no combined list any more -- land on the first type."""
    return RedirectResponse(
        relative_url_for(request, 'admin_list', word_type=WordType.parasite.value),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get(
    '/{word_type}/new',
    response_class=HTMLResponse,
    name='admin_new_word',
    dependencies=[Depends(auth.require_admin)],
)
async def new_word(request: Request, word_type: str):
    resolved = word_type_from_path(word_type)
    return templates.TemplateResponse(
        request,
        'edit.html',
        {'word': None, 'word_type': resolved, 'error': None},
    )


@router.get(
    '/{word_type}',
    response_class=HTMLResponse,
    name='admin_list',
    dependencies=[Depends(auth.require_admin)],
)
async def list_words(
    request: Request,
    word_type: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str = '',
    page: int = 1,
):
    resolved = word_type_from_path(word_type)
    page = max(page, 1)

    statement = select(Word).where(Word.type == resolved)
    count_statement = (
        select(func.count()).select_from(Word).where(Word.type == resolved)
    )

    if q:
        # Admin search is a substring match; the public API uses a prefix.
        pattern = f'%{escape_like(q.lower())}%'
        statement = statement.where(Word.word.ilike(pattern, escape='\\'))
        count_statement = count_statement.where(
            Word.word.ilike(pattern, escape='\\')
        )

    total = (await session.scalar(count_statement)) or 0
    pages = max(math.ceil(total / PAGE_SIZE), 1)
    page = min(page, pages)

    words = (
        await session.scalars(
            statement.order_by(Word.id).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
        )
    ).all()

    return templates.TemplateResponse(
        request,
        'list.html',
        {
            'words': words,
            'word_type': resolved,
            'total': total,
            'page': page,
            'pages': pages,
            'q': q,
        },
    )
