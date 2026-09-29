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
    # Mark the admin session cookie Secure (sent over HTTPS only). Compose
    # sets it from TLS, which accepts on/off as well as true/false.
    session_https_only: bool = False

    # Page size cap for the public API, mirrors the original behaviour.
    max_page_size: int = 100

    # The one-time import (duryssoile.seed) reads seed_dir, laid out as
    # data/ (the two JSON files) and audio/ (the clips).
    seed_dir: Path = Path('seed')
    upload_concurrency: int = 16

    # Relative to the working directory, /srv in the image. It cannot be
    # found from this package's location, which is site-packages once the
    # package is installed.
    alembic_ini: Path = Path('alembic.ini')

    @property
    def seed_data_dir(self) -> Path:
        return self.seed_dir / 'data'

    @property
    def seed_audio_dir(self) -> Path:
        return self.seed_dir / 'audio'

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
