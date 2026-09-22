# «Дұрыс сөйле»

Kazakh pronunciation reference: 186 parasite words and 22 008 commonly
mispronounced words, each with a pronunciation clip, served over a small HTTP
API and a Telegram bot.

This is a rewrite of the 2023 version, which is still in this repository's
history — `git show e59dfdf` for the tree it replaced. What changed:

| | 2023 version | now |
|---|---|---|
| Word data | two JSON files loaded into a dict at import | Postgres |
| Audio | read-only bind mount of a host directory | RustFS (S3-compatible object storage) |
| Editing | edit JSON, redeploy | password-protected admin page |
| Seeding | `provision.sh` + manual unzip | an idempotent `init` service that runs before the backend |

**The public API is unchanged.** Paths, query parameters and response bodies
are byte-identical to the 2023 version, verified by diffing both
implementations across every word id and page. `telegram-bot/bot.py` needed
one edit — its hardcoded service URL became configurable.

## Layout

```
backend/        FastAPI app: public API (/api/v1.0) + admin page (/admin)
init/           convergence step: schema + JSON -> Postgres, audio -> RustFS
db/schema.sql   the schema the seeder applies
data/           the two source JSON files
nginx/          reverse proxy
telegram-bot/   the 2023 bot, carried over
audio/          the pronunciation clips (git-ignored; see AUDIO_DIR)
```

## Running it

```bash
cp .env.example .env
# fill in POSTGRES_PASSWORD, RUSTFS_ACCESS_KEY, RUSTFS_SECRET_KEY,
# ADMIN_PASSWORD and SECRET_KEY -- generate each with:
#   openssl rand -base64 24

docker compose up -d --build
```

That is the whole thing. `up` starts Postgres and RustFS, runs `init` to seed
them, and only then starts the backend and nginx. The first run seeds ~22 k
words and uploads their audio, so give it a couple of minutes; later runs find
everything in sync and take a few seconds.

The clips are read from `./audio` on the host, laid out as
`./audio/<type>/<filename>.mp3`. Point `AUDIO_DIR` elsewhere if they live
somewhere else. Nothing is downloaded.

Then:

- API — <http://localhost:8080/api/v1.0/words?type=parasite>
- API docs — <http://localhost:8080/api/v1.0/docs>
- Admin — <http://localhost:8080/admin/> (log in with `ADMIN_PASSWORD`)
- RustFS console — <http://localhost:9001>

Nothing but nginx (`HTTP_PORT`, default 8080) and the RustFS console
(`RUSTFS_CONSOLE_PORT`) is published. Postgres and the S3 API stay on the
internal compose network.

## The init script

`init` is a convergence step, not a one-shot seeder. Compose runs it on every
`up`, **before the backend starts**:

```yaml
backend:
  depends_on:
    init:
      condition: service_completed_successfully
```

So the backend never serves a half-seeded dataset, and a failed init blocks
startup instead of producing a silently empty API.

### Modes

| | what it does |
|---|---|
| **sync** *(default)* | Converge, doing as little as possible. If the database and bucket already match the target, exit without writing anything — the audio directory is not even read. |
| **hard** | Drop the tables, empty the audio bucket, reseed and re-upload everything. |

```bash
docker compose up -d                        # runs sync automatically
docker compose run --rm init                # sync on demand
docker compose run --rm init --hard         # rebuild from scratch
INIT_MODE=hard docker compose up -d         # same, via the environment

docker compose run --rm init --skip-audio   # database only
docker compose run --rm init --skip-db      # audio only
```

An in-sync run takes a few seconds; a full rebuild of 22 194 clips takes
about two minutes.

### How sync decides

Two fingerprints are recorded in an `init_state` table:

- **schema fingerprint** — sha256 of `db/schema.sql`
- **data fingerprint** — sha256 of both JSON files plus the audio key scheme

| situation | what sync does |
|---|---|
| no tables yet | full seed (first run) |
| both fingerprints match | verify the bucket, then exit; nothing is written |
| data fingerprint differs | reconcile the seeded rows in place |
| schema fingerprint differs | **refuse, exit 1** |
| tables exist, no `init_state` | adopt them by reconciling |

Sync refuses on a schema change because it cannot migrate an existing schema.
Rather than guess, it tells you to run `--hard` (destructive) or to apply a
migration by hand and update `init_state.schema_fp`. Since the backend is
gated on init, this stops a deployment whose schema no longer matches its
code — deliberately.

### Reconciling, and who owns which ids

The id space is split so the two writers never collide:

- **`[0, 1000000)` — the seeder.** Ids come from the JSON.
- **`[1000000, ∞)` — the admin page.** `word_id_seq` starts at 1000000.

A reconcile only touches the seeder's range: seeded words are inserted or
updated to match the JSON, seeded words that disappeared from the JSON are
deleted, and correct versions for seeded ids are rewritten. Words created
through the admin page are never in scope, so the JSON can grow to 999 999
entries without ever overwriting one.

One consequence worth knowing: **when the JSON changes, it wins for seeded
ids.** If you edited seeded word #5 through the admin page and then changed
`data/*.json`, the reconcile resets #5 to the JSON value. While the
fingerprint is unchanged, sync writes nothing and your edit stands. Edits you
intend to keep permanently belong in `data/*.json`, or in a word you create
in the admin range.

### Audio verification

Every run lists the bucket and compares it against the database:

- a word pointing at an object that is **missing from the bucket** is always
  repaired — delete objects behind the app's back and the next sync re-uploads
  exactly those
- a word with **no audio at all** is only chased once per dataset. After a
  full pass, sync records that fingerprint, so the one word with no clip on
  disk does not cause a re-scan of all 22 k files on every startup

Listing the bucket is most of what an in-sync startup spends its time on.

### Where the audio comes from

A directory on disk, bind-mounted read-only into the init container. That is
the only supported source — Google Drive support (both the zip archive and
the folder walk) has been removed, along with the `gdown` dependency.

```
audio/
├── parasite/                 186 clips
└── commonly-mispronounced/   22 008 clips
```

`AUDIO_DIR` (default `./audio`) points at it. The directory is ~611 MB and
git-ignored. If it is missing entirely init exits 2, and because the backend
is gated on init the stack will not come up.

### How audio is matched and stored

Each JSON entry has a `filename` (e.g. `тәбет.mp3`). Init indexes every audio
file under `AUDIO_DIR` by `(parent directory, filename)` and by filename
alone, after folding both sides through the same normalisation:

- **NFC vs NFD** — filesystems disagree about how to compose Kazakh
  characters like ә and ұ; without this most files miss
- **case**
- **invisible formatting characters** — one clip on disk carries a real soft
  hyphen (U+00AD) in its name while `data/*.json` stores the literal six
  characters `\xad` for it, a double-escaping bug in the original export.
  Init decodes such escapes and then drops all Unicode `Cf` characters, which
  makes the two sides agree.

Objects are stored under an ASCII key derived from the word id,
`{type}/{id}{ext}`, not the original filename: S3 request signing and Cyrillic
keys are a bad combination, and four parasite filenames are shared by more
than one word (`әйтеуір.mp3` by three), so filenames are not unique anyway.
The original filename stays in `data/*.json`.

Words whose clip is missing keep `audio_key = NULL`; their `/audio/{id}`
returns 404 and everything else about them works. Init lists them at the end
of every full pass.

**One clip is currently missing.** Word 5939 `Ежелгі дәуір` expects
`ежелгі дәуір.mp3`, but the file on disk is `ежелгі дауир.mp3` — a different
spelling (`дауир` vs `дәуір`), not a normalisation difference. Init does not
fuzzy-match Cyrillic, so fix it by renaming the file to match the JSON, or by
changing that entry's `filename` in `data/commonly-mispronounced.json` (which
changes the data fingerprint, so the next sync reconciles it automatically).

## API

Base path `/api/v1.0`, unchanged from 2023.

```
GET /words?type=…&filter=…&offset=…&limit=…&sort=…
GET /words/{id}
GET /audio/{id}
```

- `type` — `parasite` or `commonly-mispronounced` (required)
- `filter` — case-insensitive prefix the word must start with
- `offset` — **page number, from 0.** Not a row offset. The 2023 API sliced
  `[offset*limit : (offset+1)*limit]` and the bot's arrow buttons increment it
  by one per page, so this is load-bearing; it is deliberately preserved.
- `limit` — page size, capped at `max_page_size` (100)
- `sort` — `asc` or `desc`, by id

`correctVersions` is **omitted entirely** for commonly-mispronounced words
rather than sent as `[]`, and the `incorrectUsage` / `correctUsage` keys are
omitted when a correct version has no usage example. Both match the 2023
responses exactly, which the bot depends on.

Audio is proxied through the backend rather than served as a presigned
redirect, because a presigned URL would point at the RustFS endpoint, which
is not reachable from outside the compose network.

## Admin page

`/admin/` — one password (`ADMIN_PASSWORD`), no user table. The session is a
signed cookie keyed by `SECRET_KEY`; changing `SECRET_KEY` logs everyone out.

**The interface is Kazakh only** — there is no language switch and no
fallback locale. The two type names are copied verbatim from
`telegram-bot/bot.py`, so the admin page and the bot name the same thing the
same way.

### One page per word type

There is no combined list. Each type has its own page, reachable from the
tabs in the header:

| page | contents |
|---|---|
| `/admin/parasite` | Бөгде сөздер — 186 words |
| `/admin/commonly-mispronounced` | Жиі қате айтылатын сөздер — 22 008 words |

`/admin/` redirects to the first of them. A type slug that is not one of the
two returns 404. Search, pagination and the "+ Жаңа сөз" button are all
scoped to the page you are on, and creating or deleting a word returns you to
that type's list.

Words are still edited at `/admin/words/{id}`, which is type-agnostic: the
page reads the word's own type, labels its back-link accordingly, and lets
you move a word to the other type with the Түрі selector.

### Correct versions are parasite-only

`Дұрыс нұсқалары` is shown only for `parasite`. In the source data every
`correctVersions` entry belongs to a parasite word, and the public API omits
the key entirely for the other type, so the admin page follows suit: the
editor is hidden for commonly-mispronounced words, and
`parse_correct_versions` refuses to store them against that type regardless
of what was submitted. Moving a word to commonly-mispronounced therefore
drops its correct versions. This keeps the API response shape identical to
2023, where no commonly-mispronounced word ever carried the key.

If you do want correct versions on both types, add
`WordType.commonly_mispronounced` to `TYPES_WITH_CORRECT_VERSIONS` in
`backend/app/admin/labels.py` — that one set drives both the form and the
server-side rule.

### The rest

Create and edit words, manage correct versions, and upload or delete a clip
(mp3/wav/ogg/opus/m4a/aac, 10 MB max) which goes straight into RustFS.
Deleting a word cascades to its correct versions and removes its object.

The password is compared with `secrets.compare_digest`, and five failed
attempts from one address trigger a 60-second lockout. That throttle is
per-process, so it is a speed bump rather than a real rate limiter — put the
admin page behind TLS and a network restriction before exposing it publicly,
and set `https_only=True` on the session middleware in `backend/app/main.py`
once TLS terminates in front of nginx.

## Telegram bot

Carried over from 2023 and not containerised — it needs a bot token, so it is
left for you to run:

```bash
cd telegram-bot
cp .env.example .env        # set token= and service_url=
pip install -r requirements.txt
python bot.py
```

`service_url` defaults to `http://nginx/api/v1.0` (inside compose); from the
host use `http://localhost:8080/api/v1.0`.

## Notes

- `SECRET_KEY`, `ADMIN_PASSWORD` and the RustFS keys are required; the app
  refuses to start without them instead of serving empty results.
- The backend checks Postgres and creates the RustFS bucket on startup, so a
  broken deployment fails loudly rather than returning `[]`.
- `/health` checks the database round trip and backs the compose healthcheck.
