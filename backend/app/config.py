from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (docker compose passes
    the values through from .env). Fields without a default are required --
    the app fails loudly at startup rather than serving empty results the way
    refactor_v1 did."""

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

    @property
    def database_url(self) -> str:
        return (
            f'postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}'
            f'@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}'
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
