"""Prepare the database and the bucket for the backend.

  1. Apply the migrations (alembic upgrade head).
  2. Import the seed data if the database has no words yet (duryssoile.seed).

Compose runs this as the `setup` service on every `up`, and the backend only
starts once it has exited successfully. On an existing deployment both steps
are no-ops unless a new migration has been added.

Usage:
    python -m duryssoile.bootstrap                 # migrate, seed if empty
    python -m duryssoile.bootstrap --skip-audio    # seed without uploading clips
    python -m duryssoile.bootstrap --reset         # wipe words + bucket, reimport
"""

import argparse
import asyncio
import logging
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from . import seed
from .config import get_settings

log = logging.getLogger('bootstrap')

# The revision matching the schema the pre-Alembic seeder created.
BASELINE_REVISION = '0001'


async def table_names() -> set[str]:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return set(await connection.run_sync(
                lambda sync: inspect(sync).get_table_names()
            ))
    finally:
        await engine.dispose()


def migrate() -> None:
    config = Config(str(get_settings().alembic_ini))
    tables = asyncio.run(table_names())

    # A database built before migrations existed has the tables but no record
    # of them. Mark it as being at the baseline, which reproduces exactly that
    # schema, so upgrade applies only what came after.
    if 'word' in tables and 'alembic_version' not in tables:
        log.info('adopting a pre-migration database at revision %s', BASELINE_REVISION)
        command.stamp(config, BASELINE_REVISION)

    command.upgrade(config, 'head')


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s  %(levelname)-7s %(message)s'
    )
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--skip-audio', action='store_true',
        help='import the words without uploading their clips',
    )
    parser.add_argument(
        '--reset', action='store_true',
        help='delete all words and clips and import the seed data again '
             '(discards every edit made through the admin page)',
    )
    args = parser.parse_args()

    migrate()

    try:
        asyncio.run(seed.seed(skip_audio=args.skip_audio, reset=args.reset))
    except seed.SeedError as exc:
        log.error('%s', exc)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
