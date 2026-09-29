"""One-time import of the seed data into an empty deployment.

  seed/data/parasite.json                ->  Postgres
  seed/data/commonly-mispronounced.json  ->  Postgres
  seed/audio/<type>/<filename>           ->  RustFS

The import runs only when the `word` table is empty, so it populates a fresh
deployment and is a no-op everywhere else. Once imported, the database is the
source of truth: words are edited through the admin page, and changes to the
seed files are never re-applied. `reset=True` wipes the words and the bucket
and imports again -- it discards every edit made since.

Run through duryssoile.bootstrap, which applies the migrations first.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from . import storage
from .config import get_settings
from .models import CorrectVersion, Word, WordType

log = logging.getLogger('seed')

SOURCES = {
    WordType.parasite: 'parasite.json',
    WordType.commonly_mispronounced: 'commonly-mispronounced.json',
}

CONTENT_TYPES = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.opus': 'audio/opus',
}


class SeedError(Exception):
    pass


@dataclass
class SeedWord:
    id: int
    type: WordType
    word: str
    filename: str | None
    correct_versions: list[dict] = field(default_factory=list)


def load_words(data_dir: Path) -> list[SeedWord]:
    """Read both JSON files.

    They share one id space (parasite 0-185, commonly-mispronounced
    186-22193), which the public API exposes, so ids are kept verbatim.
    """
    words: list[SeedWord] = []
    seen: dict[int, str] = {}

    for word_type, filename in SOURCES.items():
        path = data_dir / filename
        if not path.is_file():
            raise SeedError(f'missing seed file: {path}')

        entries = json.loads(path.read_text(encoding='utf-8'))
        log.info('%s: %d entries', filename, len(entries))

        for entry in entries:
            word_id = int(entry['id'])
            if word_id in seen:
                raise SeedError(
                    f'duplicate id {word_id} in {filename} (already used by {seen[word_id]})'
                )
            seen[word_id] = filename

            words.append(SeedWord(
                id=word_id,
                type=word_type,
                word=entry['word'],
                filename=entry.get('filename') or None,
                correct_versions=entry.get('correctVersions', []),
            ))

    return words


# ---------------------------------------------------------------------------
# audio: matching seed filenames to files on disk
# ---------------------------------------------------------------------------

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
       hyphen (U+00AD) in its name while the seed JSON holds the literal text
       "\\xad" for it, a double-escaping bug in the original export. Decoding
       that escape and then dropping all Unicode Cf (format) characters makes
       the two sides agree, and guards against stray zero-width joiners too.
    """
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


def resolve_audio(
    words: list[SeedWord], directory: Path
) -> tuple[dict[int, Path], list[SeedWord]]:
    """Find each word's clip. Returns ({word id: path}, words with no clip)."""
    if not directory.is_dir():
        raise SeedError(
            f'audio directory not found: {directory} -- put the clips in '
            f'seed/audio/<type>/ (or point SEED_DIR at a seed directory)'
        )

    by_pair, by_name = index_audio(directory)
    if not by_name:
        raise SeedError(f'no audio files under {directory}')
    log.info('found %d audio files under %s', len(by_name), directory)

    found: dict[int, Path] = {}
    missing: list[SeedWord] = []

    for word in words:
        if not word.filename:
            continue
        name = normalize(word.filename)
        path = by_pair.get((normalize(word.type.value), name)) or by_name.get(name)
        if path is None:
            missing.append(word)
        else:
            found[word.id] = path

    return found, missing


def upload_audio(words: list[SeedWord], paths: dict[int, Path]) -> dict[int, str]:
    """Upload the clips. Returns {word id: object key}; raises if any fail."""
    settings = get_settings()
    storage.ensure_bucket()

    def put(word: SeedWord, path: Path) -> tuple[int, str]:
        suffix = path.suffix.lower()
        key = storage.audio_key(word.type.value, word.id, suffix)
        storage.upload_file(str(path), key, CONTENT_TYPES[suffix])
        return word.id, key

    keys: dict[int, str] = {}
    failures: list[tuple[int, Exception]] = []

    with ThreadPoolExecutor(max_workers=settings.upload_concurrency) as pool:
        futures = {
            pool.submit(put, word, paths[word.id]): word.id
            for word in words
            if word.id in paths
        }
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                word_id, key = future.result()
                keys[word_id] = key
            except Exception as exc:  # noqa: BLE001 - reported below
                failures.append((futures[future], exc))
            if done % 1000 == 0:
                log.info('uploaded %d/%d', done, len(futures))

    if failures:
        for word_id, exc in failures[:5]:
            log.error('upload failed for word %d: %s', word_id, exc)
        # Nothing has been written to the database yet, so rerunning retries
        # the whole import; uploads overwrite the same keys.
        raise SeedError(f'{len(failures)} of {len(futures)} uploads failed')

    log.info('uploaded %d clips to bucket %r', len(keys), settings.rustfs_bucket)
    return keys


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

async def insert_words(
    session: AsyncSession, words: list[SeedWord], keys: dict[int, str]
) -> None:
    await session.execute(insert(Word), [
        {'id': w.id, 'type': w.type, 'word': w.word, 'audio_key': keys.get(w.id)}
        for w in words
    ])
    await session.execute(insert(CorrectVersion), [
        {
            'word_id': w.id,
            'word': version['word'],
            'incorrect_usage': version.get('incorrectUsage'),
            'correct_usage': version.get('correctUsage'),
            'position': position,
        }
        for w in words
        for position, version in enumerate(w.correct_versions)
    ])
    # Seeded ids bypass word_id_seq. It starts far above them, but move it
    # past the highest one anyway so a large seed file cannot collide with the
    # first word created through the admin page.
    await session.execute(text(
        "SELECT setval('word_id_seq', (SELECT max(id) FROM word)) "
        "WHERE (SELECT max(id) FROM word) >= (SELECT last_value FROM word_id_seq)"
    ))


async def seed(*, skip_audio: bool = False, reset: bool = False) -> None:
    settings = get_settings()
    words = load_words(settings.seed_data_dir)

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            count = await session.scalar(select(func.count()).select_from(Word))

            if count and not reset:
                log.info('database already holds %d words -- nothing to import', count)
                return

            # Match the clips before touching anything, so a wrong audio path
            # fails the run while the existing data is still intact.
            paths: dict[int, Path] = {}
            if skip_audio:
                log.info('skipping audio: every word is imported without a clip')
            else:
                paths, missing = resolve_audio(words, settings.seed_audio_dir)
                log.info('matched %d clips, %d words without one', len(paths), len(missing))
                for word in missing:
                    log.warning('no clip for word %d: %s', word.id, word.filename)

            if reset:
                log.warning('reset: deleting %d words and emptying the bucket', count)
                await session.execute(delete(Word))  # cascades to correct_version
                await session.commit()
                removed = await run_in_threadpool(storage.empty_bucket)
                log.warning('deleted %d objects', removed)

            keys: dict[int, str] = {}
            if paths:
                keys = await run_in_threadpool(upload_audio, words, paths)

            await insert_words(session, words, keys)
            await session.commit()

        log.info(
            'imported %d words (%d with audio) and %d correct versions',
            len(words), len(keys), sum(len(w.correct_versions) for w in words),
        )
    finally:
        await engine.dispose()
