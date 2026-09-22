#!/usr/bin/env python
"""Bring «Дұрыс сөйле» to the state described by db/schema.sql and data/*.json.

Two modes:

  sync (default)  Converge, doing as little as possible. If the database
                  already matches the target, exit without touching anything
                  -- no reads of the audio directory, no uploads, no writes.
                  This is what runs on every `docker compose up`.

  hard (--hard)   Rebuild from scratch: drop the tables, empty the audio
                  bucket, reseed and re-upload everything.

Sync decides what to do by comparing two fingerprints against the ones
recorded in the `init_state` table:

  schema fingerprint   sha256 of db/schema.sql
  data fingerprint     sha256 of both JSON files + the audio key scheme

If the schema fingerprint changed, sync refuses and asks for --hard, because
it cannot migrate an existing schema. If only the data fingerprint changed it
reconciles the seeded rows in place, leaving anything created through the
admin page alone. Audio objects are verified against the bucket on every run
and repaired if any went missing.

Usage:
    docker compose run --rm init                # sync
    docker compose run --rm init --hard         # rebuild everything
    docker compose run --rm init --skip-audio   # database only
    docker compose run --rm init --skip-db      # audio only
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s  %(levelname)-7s %(message)s'
)
log = logging.getLogger('init')

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = Path(os.getenv('SCHEMA_PATH', ROOT / 'db' / 'schema.sql'))
DATA_DIR = Path(os.getenv('DATA_DIR', ROOT / 'data'))
# Audio is read from a directory on disk, bind-mounted by compose from
# ./audio. Files are expected at <AUDIO_DIR>/<type>/<filename>, matching
# the `filename` field in data/*.json.
AUDIO_DIR = Path(os.getenv('AUDIO_DIR', ROOT / 'audio'))

SOURCES = {
    'parasite': 'parasite.json',
    'commonly-mispronounced': 'commonly-mispronounced.json',
}

# Bump when the object key layout changes: it forces a re-upload by changing
# the data fingerprint, because the old keys no longer describe the target.
KEY_SCHEME_VERSION = '1'

CONTENT_TYPES = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.opus': 'audio/opus',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
}

APP_TABLES = ('word', 'correct_version')

# Ids below this belong to the seeder (data/*.json); ids at or above it are
# handed out by word_id_seq to words created through the admin page. Keeping
# the two ranges disjoint means reconciling a grown JSON file can never
# overwrite somebody's admin entry.
ADMIN_ID_BASE = 1_000_000


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        log.error('%s is not set -- check your .env file', name)
        sys.exit(2)
    return value or ''


def dsn() -> str:
    return (
        f"postgresql://{env('POSTGRES_USER', 'duryssoile')}:"
        f"{env('POSTGRES_PASSWORD', required=True)}@"
        f"{env('POSTGRES_HOST', 'db')}:{env('POSTGRES_PORT', '5432')}/"
        f"{env('POSTGRES_DB', 'duryssoile')}"
    )


def s3_client():
    return boto3.client(
        's3',
        endpoint_url=env('RUSTFS_ENDPOINT', 'http://rustfs:9000'),
        aws_access_key_id=env('RUSTFS_ACCESS_KEY', required=True),
        aws_secret_access_key=env('RUSTFS_SECRET_KEY', required=True),
        region_name=env('RUSTFS_REGION', 'us-east-1'),
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
            # Must cover UPLOAD_CONCURRENCY or botocore keeps discarding and
            # re-opening connections ("Connection pool is full") under load.
            max_pool_connections=max(int(env('UPLOAD_CONCURRENCY', '16')) + 4, 10),
        ),
    )


# ---------------------------------------------------------------------------
# target: what the JSON files say the database should contain
# ---------------------------------------------------------------------------

@dataclass
class Target:
    words: list[tuple]                # (id, type, word, audio_key=None)
    correct_versions: list[tuple]     # (word_id, word, incorrect, correct, pos)
    filenames: dict[int, str]         # word id -> source audio filename
    data_fingerprint: str
    schema_fingerprint: str

    @property
    def max_id(self) -> int:
        return max((row[0] for row in self.words), default=-1)

    @property
    def ids(self) -> set[int]:
        return {row[0] for row in self.words}


def load_target() -> Target:
    """Read both JSON files and the schema, and fingerprint them.

    The two files share one id space (parasite 0-185,
    commonly-mispronounced 186-22193), which the public API depends on, so
    ids are carried over verbatim rather than regenerated.
    """
    if not SCHEMA_PATH.is_file():
        log.error('missing schema: %s', SCHEMA_PATH)
        sys.exit(2)

    words: list[tuple] = []
    correct: list[tuple] = []
    filenames: dict[int, str] = {}
    seen: dict[int, str] = {}

    data_hash = hashlib.sha256()
    data_hash.update(f'key-scheme={KEY_SCHEME_VERSION}\n'.encode())

    for word_type, filename in SOURCES.items():
        path = DATA_DIR / filename
        if not path.is_file():
            log.error('missing data file: %s', path)
            sys.exit(2)

        raw = path.read_bytes()
        data_hash.update(f'{filename}:'.encode())
        data_hash.update(hashlib.sha256(raw).digest())

        entries = json.loads(raw.decode('utf-8'))
        log.info('%s: %d entries', filename, len(entries))

        for entry in entries:
            word_id = int(entry['id'])

            if word_id in seen:
                log.error(
                    'duplicate id %d in %s (already used by %s)',
                    word_id, filename, seen[word_id],
                )
                sys.exit(2)
            if word_id >= ADMIN_ID_BASE:
                log.error(
                    'id %d in %s is inside the admin range (>= %d); the seeded '
                    'data must stay below it',
                    word_id, filename, ADMIN_ID_BASE,
                )
                sys.exit(2)
            seen[word_id] = filename

            words.append((word_id, word_type, entry['word'], None))

            if entry.get('filename'):
                filenames[word_id] = entry['filename']

            for position, version in enumerate(entry.get('correctVersions', [])):
                correct.append((
                    word_id,
                    version['word'],
                    version.get('incorrectUsage'),
                    version.get('correctUsage'),
                    position,
                ))

    return Target(
        words=words,
        correct_versions=correct,
        filenames=filenames,
        data_fingerprint=data_hash.hexdigest(),
        schema_fingerprint=hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
    )


# ---------------------------------------------------------------------------
# recorded state
# ---------------------------------------------------------------------------

INIT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS init_state (
    -- Single-row table: the CHECK pins the primary key to one value.
    id             boolean PRIMARY KEY DEFAULT true CHECK (id),
    schema_fp      text NOT NULL,
    data_fp        text NOT NULL,
    -- data_fp as of the last completed audio pass. When it equals data_fp,
    -- words still lacking audio are known to have no clip in the source, so
    -- sync does not re-download looking for them.
    audio_fp       text,
    max_seeded_id  integer NOT NULL,
    word_count     integer NOT NULL,
    mode           text NOT NULL,
    applied_at     timestamptz NOT NULL DEFAULT now()
)
"""


async def wait_for_postgres(timeout: int = 60) -> asyncpg.Connection:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Exception | None = None

    while loop.time() < deadline:
        try:
            return await asyncpg.connect(dsn())
        except (OSError, asyncpg.PostgresError) as exc:
            last = exc
            await asyncio.sleep(1)

    log.error('postgres unreachable after %ds: %s', timeout, last)
    sys.exit(1)


async def read_state(conn: asyncpg.Connection) -> asyncpg.Record | None:
    await conn.execute(INIT_STATE_DDL)
    return await conn.fetchrow('SELECT * FROM init_state WHERE id')


async def write_state(
    conn: asyncpg.Connection,
    target: Target,
    mode: str,
    *,
    audio_fp: str | None,
) -> None:
    count = await conn.fetchval('SELECT count(*) FROM word')
    await conn.execute(
        """
        INSERT INTO init_state (
            id, schema_fp, data_fp, audio_fp, max_seeded_id, word_count, mode,
            applied_at
        ) VALUES (true, $1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (id) DO UPDATE SET
            schema_fp = excluded.schema_fp,
            data_fp = excluded.data_fp,
            audio_fp = excluded.audio_fp,
            max_seeded_id = excluded.max_seeded_id,
            word_count = excluded.word_count,
            mode = excluded.mode,
            applied_at = excluded.applied_at
        """,
        target.schema_fingerprint,
        target.data_fingerprint,
        audio_fp,
        target.max_id,
        count,
        mode,
    )


async def app_tables_exist(conn: asyncpg.Connection) -> bool:
    found = await conn.fetchval(
        'SELECT count(*) FROM information_schema.tables '
        'WHERE table_schema = current_schema() AND table_name = ANY($1::text[])',
        list(APP_TABLES),
    )
    return found == len(APP_TABLES)


# ---------------------------------------------------------------------------
# database: full rebuild
# ---------------------------------------------------------------------------

async def rebuild_database(conn: asyncpg.Connection, target: Target) -> None:
    """Apply the schema (dropping what is there) and bulk-load the target."""
    async with conn.transaction():
        log.info('applying %s', SCHEMA_PATH)
        await conn.execute(SCHEMA_PATH.read_text(encoding='utf-8'))

        # COPY rather than INSERT: 22k rows in one round trip.
        await conn.copy_records_to_table(
            'word',
            records=target.words,
            columns=['id', 'type', 'word', 'audio_key'],
        )
        await conn.copy_records_to_table(
            'correct_version',
            records=target.correct_versions,
            columns=[
                'word_id', 'word', 'incorrect_usage', 'correct_usage', 'position'
            ],
        )
        await sync_sequences(conn)

    log.info(
        'seeded %d words and %d correct versions',
        len(target.words), len(target.correct_versions),
    )


async def sync_sequences(conn: asyncpg.Connection) -> None:
    """Move the sequences past the seeded ids.

    The seeded ids come from the JSON and bypass word_id_seq, so without this
    the first word created through the admin page collides on the primary key.
    """
    await conn.execute(
        "SELECT setval('word_id_seq', GREATEST("
        "  (SELECT coalesce(max(id), 0) FROM word), $1::bigint), true)",
        ADMIN_ID_BASE,
    )
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('correct_version', 'id'),"
        "  (SELECT coalesce(max(id), 1) FROM correct_version), true)"
    )


# ---------------------------------------------------------------------------
# database: in-place reconcile
# ---------------------------------------------------------------------------

async def reconcile_database(conn: asyncpg.Connection, target: Target, state) -> None:
    """Converge the seeded rows without dropping anything.

    Only ids the seeder owns are touched. Words created through the admin page
    draw ids above the seeded range and are left alone; a seeded word that has
    disappeared from the JSON is removed.
    """
    async with conn.transaction():
        await conn.execute(
            'CREATE TEMP TABLE target_word ('
            '  id integer PRIMARY KEY, type word_type NOT NULL, word text NOT NULL'
            ') ON COMMIT DROP'
        )
        await conn.copy_records_to_table(
            'target_word',
            records=[(row[0], row[1], row[2]) for row in target.words],
            columns=['id', 'type', 'word'],
        )

        inserted = await conn.fetchval(
            """
            WITH changed AS (
                INSERT INTO word (id, type, word)
                SELECT id, type, word FROM target_word
                ON CONFLICT (id) DO UPDATE
                    SET type = excluded.type, word = excluded.word
                    WHERE word.type IS DISTINCT FROM excluded.type
                       OR word.word IS DISTINCT FROM excluded.word
                RETURNING 1
            )
            SELECT count(*) FROM changed
            """
        )

        # Seeded rows that are no longer in the JSON. Scoped to the seeder's
        # id range, so admin-created words are never in scope.
        removed = await conn.fetchval(
            'WITH gone AS ('
            '  DELETE FROM word w WHERE w.id < $1'
            '   AND NOT EXISTS (SELECT 1 FROM target_word t WHERE t.id = w.id)'
            '  RETURNING 1'
            ') SELECT count(*) FROM gone',
            ADMIN_ID_BASE,
        )

        # Correct versions are small (216 rows) and belong wholly to the
        # seeder, so replacing them outright is simpler than diffing.
        await conn.execute(
            'DELETE FROM correct_version WHERE word_id IN (SELECT id FROM target_word)'
        )
        await conn.copy_records_to_table(
            'correct_version',
            records=target.correct_versions,
            columns=[
                'word_id', 'word', 'incorrect_usage', 'correct_usage', 'position'
            ],
        )

        await sync_sequences(conn)

    log.info(
        'reconciled: %d words inserted or updated, %d removed, '
        '%d correct versions rewritten',
        inserted, removed, len(target.correct_versions),
    )


# ---------------------------------------------------------------------------
# audio: bucket inspection
# ---------------------------------------------------------------------------

def ensure_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        log.info('creating bucket %r', bucket)
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') not in (
                'BucketAlreadyOwnedByYou', 'BucketAlreadyExists',
            ):
                raise


def list_bucket_keys(client, bucket: str) -> set[str]:
    keys: set[str] = set()
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        keys.update(item['Key'] for item in page.get('Contents', []))
    return keys


def empty_bucket(client, bucket: str) -> int:
    """Delete every object. Used by --hard."""
    keys = sorted(list_bucket_keys(client, bucket))
    if not keys:
        return 0

    for start in range(0, len(keys), 1000):
        batch = keys[start:start + 1000]
        client.delete_objects(
            Bucket=bucket,
            Delete={'Objects': [{'Key': key} for key in batch], 'Quiet': True},
        )

    log.info('emptied bucket %r (%d objects)', bucket, len(keys))
    return len(keys)


# ---------------------------------------------------------------------------
# audio: acquiring the files
# ---------------------------------------------------------------------------

def acquire_audio() -> Path:
    """Return the directory holding the pronunciation clips.

    The clips live on disk and are bind-mounted into this container; nothing
    is downloaded. Layout is <AUDIO_DIR>/<type>/<filename>, where <filename>
    is the `filename` field from data/*.json, but a clip found anywhere under
    AUDIO_DIR is matched on its basename too (see index_audio).
    """
    if not AUDIO_DIR.is_dir():
        log.error(
            'AUDIO_DIR is not a directory: %s -- compose bind-mounts ./audio '
            'here, so create it (and populate it) on the host first',
            AUDIO_DIR,
        )
        sys.exit(2)

    log.info('reading audio from %s', AUDIO_DIR)
    return AUDIO_DIR


# Matches a literal backslash-x-hex escape left in the data as text, e.g.
# the six characters \xad instead of the soft hyphen they were meant to encode.
_LITERAL_ESCAPE = re.compile(r'\\x([0-9a-fA-F]{2})')


def normalize(text: str) -> str:
    """Fold a filename to one comparable form.

    Three separate mismatches to absorb:

    1. NFC vs NFD -- filesystems disagree about how to compose Kazakh
       characters like ә and ұ, so a naive compare misses most files.
    2. Case.
    3. Invisible formatting characters. One clip on disk carries a real soft
       hyphen (U+00AD) in its name while data/*.json holds the literal text
       "\xad" for it, a double-escaping bug in the original export. Decoding
       that escape and then dropping all Unicode Cf (format) characters makes
       the two sides agree, and guards against stray zero-width joiners too.
    """
    # Decode escapes that leaked into the data as literal text.
    text = _LITERAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    text = unicodedata.normalize('NFC', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf')
    return text.casefold()


def index_audio(directory: Path) -> tuple[dict[tuple[str, str], Path], dict[str, Path]]:
    """Index every audio file by (parent directory, filename) and by filename."""
    by_pair: dict[tuple[str, str], Path] = {}
    by_name: dict[str, Path] = {}

    for path in directory.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in CONTENT_TYPES:
            continue

        name = normalize(path.name)
        by_pair.setdefault((normalize(path.parent.name), name), path)
        # A bare filename can repeat across the two type folders; first wins
        # and the pair lookup above is what actually disambiguates.
        by_name.setdefault(name, path)

    return by_pair, by_name


def upload_audio(
    directory: Path, wanted: list[tuple[int, str, str]], bucket: str, client
) -> tuple[list[tuple[str, int]], list[tuple[int, str]]]:
    """Upload the requested clips. Returns (updates, missing).

    `wanted` is (word_id, word_type, source_filename).
    `updates` is (audio_key, word_id) ready for the UPDATE.
    """
    by_pair, by_name = index_audio(directory)
    log.info('found %d audio files under %s', len(by_name), directory)

    resolved: list[tuple[int, str, Path]] = []
    missing: list[tuple[int, str]] = []

    for word_id, word_type, filename in wanted:
        name = normalize(filename)
        path = by_pair.get((normalize(word_type), name)) or by_name.get(name)

        if path is None:
            missing.append((word_id, filename))
            continue

        resolved.append((word_id, word_type, path))

    log.info('matched %d clips, %d unmatched', len(resolved), len(missing))

    def put(word_id: int, word_type: str, path: Path) -> tuple[str, int]:
        # ASCII key derived from the id: S3 signing and Cyrillic filenames are
        # a bad mix, and this matches the keys the admin page writes.
        key = f'{word_type}/{word_id}{path.suffix.lower()}'
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={'ContentType': CONTENT_TYPES[path.suffix.lower()]},
        )
        return key, word_id

    updates: list[tuple[str, int]] = []
    failures = 0
    done = 0

    workers = int(env('UPLOAD_CONCURRENCY', '16'))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(put, word_id, word_type, path): word_id
            for word_id, word_type, path in resolved
        }
        for future in as_completed(futures):
            try:
                updates.append(future.result())
            except Exception as exc:  # noqa: BLE001
                failures += 1
                if failures <= 5:
                    log.warning('upload failed for word %s: %s', futures[future], exc)

            done += 1
            if done % 1000 == 0:
                log.info('uploaded %d/%d', done, len(resolved))

    if failures:
        log.warning('%d uploads failed', failures)

    log.info('uploaded %d objects to bucket %r', len(updates), bucket)
    return updates, missing


# ---------------------------------------------------------------------------
# audio: the step itself
# ---------------------------------------------------------------------------

async def audio_step(
    conn: asyncpg.Connection, target: Target, state, *, force: bool
) -> bool:
    """Make the bucket match the target. Returns True if a full pass completed.

    A full pass means every word that could have audio was considered, so the
    caller may record audio_fp and skip the download next time.
    """
    bucket = env('RUSTFS_BUCKET', 'audio')
    client = s3_client()
    ensure_bucket(client, bucket)

    present = list_bucket_keys(client, bucket)
    log.info('bucket %r holds %d objects', bucket, len(present))

    rows = await conn.fetch(
        'SELECT id, type::text AS type, audio_key FROM word '
        'WHERE id = ANY($1::int[]) ORDER BY id',
        sorted(target.ids),
    )

    # Split the work by why a word lacks a usable object.
    broken: list[tuple[int, str, str]] = []   # linked but the object is gone
    unlinked: list[tuple[int, str, str]] = []  # never linked

    for row in rows:
        filename = target.filenames.get(row['id'])
        if not filename:
            continue

        if row['audio_key'] is None:
            unlinked.append((row['id'], row['type'], filename))
        elif row['audio_key'] not in present:
            broken.append((row['id'], row['type'], filename))

    if not broken and not unlinked:
        log.info('audio in sync: every word with a clip has its object')
        return True

    if broken:
        log.warning(
            '%d words point at an object that is missing from the bucket', len(broken)
        )

    # Words that were never linked are either genuinely absent from the source
    # or not uploaded yet. If a full pass already ran for this exact dataset,
    # the former is the answer and re-downloading would achieve nothing.
    already_tried = state is not None and state['audio_fp'] == target.data_fingerprint

    if unlinked and already_tried and not force:
        log.info(
            '%d words have no clip in the source data (unchanged since the last '
            'full pass) -- skipping the download', len(unlinked)
        )
        unlinked = []

    if not broken and not unlinked:
        return True

    wanted = broken + unlinked
    log.info('%d clips to fetch', len(wanted))

    directory = acquire_audio()
    updates, missing = upload_audio(directory, wanted, bucket, client)

    if updates:
        async with conn.transaction():
            await conn.executemany(
                'UPDATE word SET audio_key = $1 WHERE id = $2', updates
            )

    linked = await conn.fetchval(
        'SELECT count(*) FROM word WHERE audio_key IS NOT NULL'
    )
    log.info('%d words now have audio', linked)

    if missing:
        log.warning(
            '%d words have no audio file on disk (first few: %s)',
            len(missing), ', '.join(name for _, name in missing[:5]),
        )
        log.warning(
            'Their /audio/{id} returns 404; everything else about them works.'
        )

    # A full pass happened iff we considered every unlinked word, i.e. we did
    # not short-circuit above.
    return not already_tried or force or bool(broken)


# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Seed and converge the Дұрыс сөйле database and audio bucket.'
    )
    parser.add_argument(
        '--hard',
        action='store_true',
        default=env('INIT_MODE', 'sync').strip().lower() == 'hard',
        help='drop the tables and the bucket and rebuild from scratch '
             '(default: read INIT_MODE, which is "sync")',
    )
    parser.add_argument('--skip-db', action='store_true', help='leave the database alone')
    parser.add_argument(
        '--skip-audio', action='store_true', help='do not fetch or upload audio'
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    mode = 'hard' if args.hard else 'sync'

    target = load_target()
    conn = await wait_for_postgres()

    try:
        state = await read_state(conn)
        tables = await app_tables_exist(conn)

        if args.skip_db:
            plan = 'skip'
        elif mode == 'hard':
            plan = 'rebuild'
        elif not tables:
            log.info('no application tables found -- first run')
            plan = 'rebuild'
        elif state is None:
            log.info('tables exist but no init_state row -- adopting them')
            plan = 'reconcile'
        elif state['schema_fp'] != target.schema_fingerprint:
            log.error(
                'db/schema.sql has changed since the database was built '
                '(recorded %s, now %s).',
                state['schema_fp'][:12], target.schema_fingerprint[:12],
            )
            log.error(
                'Sync cannot migrate an existing schema. Re-run with --hard to '
                'rebuild from scratch (this discards admin edits), or apply a '
                'migration by hand and update init_state.schema_fp.'
            )
            return 1
        elif state['data_fp'] != target.data_fingerprint:
            log.info(
                'data/*.json has changed (recorded %s, now %s) -- reconciling',
                state['data_fp'][:12], target.data_fingerprint[:12],
            )
            plan = 'reconcile'
        else:
            plan = 'verify'

        log.info('mode=%s plan=%s', mode, plan)

        if plan == 'rebuild':
            if not args.skip_audio and mode == 'hard':
                # --hard means the bucket is rebuilt too, so clear it first.
                client = s3_client()
                bucket = env('RUSTFS_BUCKET', 'audio')
                ensure_bucket(client, bucket)
                empty_bucket(client, bucket)
            await rebuild_database(conn, target)
        elif plan == 'reconcile':
            await reconcile_database(conn, target, state)
        elif plan == 'verify':
            counted = await conn.fetchval('SELECT count(*) FROM word')
            log.info(
                'database already matches the target (%d words, fingerprint %s)',
                counted, target.data_fingerprint[:12],
            )

        if args.skip_audio:
            log.info('--skip-audio: leaving the bucket untouched')
            if not args.skip_db:
                # audio_fp is deliberately preserved: this run made no claim
                # about the bucket.
                await write_state(
                    conn, target, mode,
                    audio_fp=state['audio_fp'] if state else None,
                )
            log.info('done')
            return 0

        full_pass = await audio_step(
            conn, target, state, force=(plan == 'rebuild')
        )

        if not args.skip_db:
            await write_state(
                conn, target, mode,
                audio_fp=target.data_fingerprint if full_pass else (
                    state['audio_fp'] if state else None
                ),
            )
    finally:
        await conn.close()

    log.info('done')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
