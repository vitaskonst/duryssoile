import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from . import storage
from .admin import auth as admin_auth
from .admin.router import router as admin_router
from .api.audio import router as audio_router
from .api.words import router as words_router
from .config import get_settings
from .db import engine

logger = logging.getLogger('duryssoile')

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on a broken deployment instead of serving empty results.
    async with engine.connect() as connection:
        await connection.execute(text('SELECT 1'))
    logger.info('postgres reachable')

    await run_in_threadpool(storage.ensure_bucket)
    logger.info('rustfs bucket %r ready', settings.rustfs_bucket)

    yield

    await engine.dispose()


app = FastAPI(title='Дұрыс сөйле', lifespan=lifespan)

# Signs the admin session cookie.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie='duryssoile_admin',
    max_age=14 * 24 * 3600,
    same_site='lax',
    https_only=False,  # set True once TLS terminates in front of nginx
)

app.add_exception_handler(
    admin_auth.RedirectToLogin, admin_auth.redirect_to_login_handler
)

# The public API lives in its own sub-application so it keeps the original
# URL space (/api/v1.0/words, /api/v1.0/audio/{id}) and gets its own
# OpenAPI docs at /api/v1.0/docs. Existing clients talk to exactly these
# paths, unchanged from the 2023 version.
api_v1 = FastAPI(title='Дұрыс сөйле API', version='1.0')
api_v1.include_router(words_router, prefix='/words', tags=['Words'])
api_v1.include_router(audio_router, prefix='/audio', tags=['Audio'])

app.mount('/api/v1.0', api_v1)

app.include_router(admin_router, prefix='/admin')


@app.get('/health', tags=['Meta'])
async def health():
    async with engine.connect() as connection:
        await connection.execute(text('SELECT 1'))
    return {'status': 'ok'}


@app.get('/', include_in_schema=False)
async def root():
    return RedirectResponse('/admin/')
