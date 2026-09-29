from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (docker compose passes
    the values through from .env). Fields without a default are required --
    the app fails loudly at startup rather than serving empty results."""

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    postgres_host: str = 'db'
    postgres_port: int = 5432
    postgres_db: str = 'duryssoile'
    postgres_user: str = 'duryssoile'
    postgres_password: str

    rustfs_endpoint: str = 'http://rustfs:9000'
    rustfs_access_key: str
    rustfs_secret_key: str
    rustfs_bucket: str = 'audio'
    rustfs_region: str = 'us-east-1'

    admin_password: str
    secret_key: str

    # Page size cap for the public API, mirrors the original behaviour.
    max_page_size: int = 100

    # The one-time import (app.seed): the two JSON files live in seed_dir and
    # the clips in audio_dir, which defaults to seed_dir/audio.
    seed_dir: Path = Path('seed')
    audio_dir: Path | None = None
    upload_concurrency: int = 16

    @property
    def seed_audio_dir(self) -> Path:
        return self.audio_dir or self.seed_dir / 'audio'

    @property
    def database_url(self) -> str:
        # Percent-encode the credentials: a generated password containing
        # '/', '@' or ':' would otherwise silently corrupt the DSN.
        user = quote(self.postgres_user, safe='')
        password = quote(self.postgres_password, safe='')
        return (
            f'postgresql+asyncpg://{user}:{password}'
            f'@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}'
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
