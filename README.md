# «Дұрыс сөйле»

Kazakh pronunciation reference: 186 parasite words and 22 008 commonly
mispronounced words, each with a pronunciation clip, served over a small HTTP
API. This repository is the backend only; the clients live in their own
repositories and are pointed at this API by configuration:

- Telegram bot — https://github.com/vitaskonst/durys-soile-bot
- Android app — https://github.com/vitaskonst/durys-soile-mobile

This is a rewrite of the 2023 version, which is still in this repository's
history — `git show e59dfdf` for the tree it replaced. What changed:

| | 2023 version | now |
|---|---|---|
| Word data | two JSON files loaded into a dict at import | Postgres, schema managed by Alembic |
| Audio | read-only bind mount of a host directory | RustFS (S3-compatible object storage) |
| Editing | edit JSON, redeploy | password-protected admin page |
| Seeding | `provision.sh` + manual unzip | a one-time import that runs on first start |

**The public API is unchanged.** Paths, query parameters and response bodies
are byte-identical to the 2023 version, verified by diffing both
implementations across every word id and page, so existing clients keep
working as-is.

## Layout

```
app/                    the application; the image's build context
  Dockerfile            one image, used by both the backend and setup services
  pyproject.toml        package metadata; pinned dependencies in requirements.txt
  alembic.ini
  migrations/           Alembic migrations -- the schema lives here
  src/duryssoile/       the package: public API (/api/v1.0) + admin page (/admin)
    bootstrap.py        pre-start step: apply migrations, import the seed if empty
    seed.py             the one-time import: seed/ -> Postgres + RustFS
seed/                   the original data: two JSON files + audio/ (git-ignored)
caddy/                  reverse proxy, and HTTPS with automatic certificates
docker-compose.yml      the stack; configured by .env (see .env.example)
```

`app/` knows nothing about how it is deployed: the compose file, the Caddy
config and `.env` live outside it, and the seed data is mounted in at run
time.

## Running it

```bash
cp .env.example .env
# fill in POSTGRES_PASSWORD, RUSTFS_ACCESS_KEY, RUSTFS_SECRET_KEY,
# ADMIN_PASSWORD and SECRET_KEY (see .env.example for how to generate them)

# put the clips in seed/audio/<type>/ (see seed/audio/README.md)

docker compose up -d --build
```

`up` starts Postgres and RustFS, runs `setup` (migrations, then the seed
import on a fresh database), and only then starts the backend and Caddy. The
first run imports ~22 k words and uploads their audio, so give it a couple of
minutes; later runs find the database populated and take a second or two.

Then:

- API — <http://localhost:8080/api/v1.0/words?type=parasite>
- API docs — <http://localhost:8080/api/v1.0/docs>
- Admin — <http://localhost:8080/admin/> (log in with `ADMIN_PASSWORD`)
- RustFS console — <http://localhost:9001>

Nothing but Caddy (`HTTP_PORT`/`HTTPS_PORT`, default 8080/8443) and the
RustFS console (`RUSTFS_CONSOLE_PORT`, `127.0.0.1:9001` — the host's
loopback only; use an SSH tunnel to reach it on a server) is published.
Postgres, the S3 API and the backend itself stay on the internal compose
network.

## HTTPS

Caddy (`caddy/`) is the reverse proxy. `TLS` in `.env` switches it between
two modes:

- `TLS=off` (the default): plain HTTP on port 80. For local development.
- `TLS=on`: HTTPS for `DOMAIN`. Caddy obtains a Let's Encrypt certificate on
  first start and renews it on its own, and every plain-HTTP request is
  redirected to HTTPS. `DOMAIN` must resolve to the host, and port 443
  (`HTTPS_PORT=443`) must be reachable from the internet: Let's Encrypt
  validates over it (the TLS-ALPN challenge). Port 80 (`HTTP_PORT=80`) is
  only for the redirect. It also marks the admin session cookie `Secure`.

  The HTTP challenge is disabled in `caddy/sites/tls-on.caddy` because the
  university's perimeter firewall blocks it: a request with Let's Encrypt's
  validation User-Agent to a `/.well-known/acme-challenge/` path gets a 503
  "Application Blocked" page and never reaches Caddy.

```bash
# production .env
TLS=on
DOMAIN=duryssoile.nu.edu.kz
HTTP_PORT=80
HTTPS_PORT=443
```

**The redirect is a 301, deliberately.** Caddy's built-in HTTP-to-HTTPS
redirect is a 308, and the published v1 Android app plays audio by handing
the `http://…/audio/{id}` URL to Android's MediaPlayer, which follows a 301
from http to https but treats a 308 as an error — the word list loads and
the audio silently never plays. Tested on Android 15 against both the v1
and v2 apps; the v2 app and the Telegram bot follow either code. So
`caddy/Caddyfile` turns the automatic redirect off and
`caddy/sites/tls-on.caddy` sends a 301 itself. Do not "simplify" it back.

The certificates live in the `caddy_data` volume. Keep it: recreating it
means requesting new certificates, and Let's Encrypt rate-limits those.

## Schema and migrations

The schema is defined by the Alembic migrations in `app/migrations/versions/`.
`setup` runs `alembic upgrade head` on every `up`, so deploying a new
migration is just deploying the code. The models in
`app/src/duryssoile/models.py` mirror
the schema, index and constraint names included.

To change the schema, edit the models, rebuild, and generate a migration.
Alembic compares the models against the running database, and the mount puts
the generated file in your checkout rather than inside the container:

```bash
docker compose build
docker compose run --rm --user "$(id -u)" -v ./app/migrations:/srv/migrations \
    --entrypoint alembic setup revision --autogenerate --rev-id 0003 -m "describe it"
```

Always review what it generates. It cannot compare the functional index
`word_type_lower_word_idx` (it logs that it skips it), so changes to that
index have to be written by hand. `up` applies the new migration.

To confirm the models and the migrations agree:

```bash
docker compose run --rm --entrypoint alembic setup check
```

A database built before migrations existed (by the old `init` service) has
the tables but no `alembic_version`. `duryssoile.bootstrap` recognises that and
stamps it at revision `0001`, which reproduces exactly that schema, before
upgrading.

## Seed data

`seed/` holds the data the 2023 version served:

```
seed/
├── data/
│   ├── parasite.json                  186 words
│   └── commonly-mispronounced.json    22 008 words
└── audio/                             git-ignored, ~611 MB
    ├── parasite/
    └── commonly-mispronounced/
```

It is imported **once**, into an empty database. After that the database is
the source of truth: words are edited through the admin page, and changing
the seed files has no effect on a running deployment. Back up the database
and the bucket, not `seed/`.

The import reads `./seed`; set `SEED_DIR` to use another host directory with
the same `data/` + `audio/` layout. It is only read during the import, so a
deployment that has been seeded no longer needs it.

```bash
docker compose up -d                          # imports if the database is empty
docker compose run --rm setup --skip-audio    # import without the clips
docker compose run --rm setup --reset         # wipe everything and reimport
```

`--reset` deletes every word and every object in the bucket before importing,
so it discards all edits made through the admin page. The clips are matched
before anything is deleted, so a wrong `SEED_DIR` fails the run with the
existing data intact.

The import writes nothing to the database until every upload has succeeded,
so a failed run can simply be repeated.

### How audio is matched and stored

Each JSON entry has a `filename` (e.g. `тәбет.mp3`). The import indexes every
audio file under the audio directory by `(parent directory, filename)` and by
filename alone, after folding both sides through the same normalisation:

- **NFC vs NFD** — filesystems disagree about how to compose Kazakh
  characters like ә and ұ; without this most files miss
- **case**
- **invisible formatting characters** — one clip on disk carries a real soft
  hyphen (U+00AD) in its name while `seed/data/*.json` stores the literal six
  characters `\xad` for it, a double-escaping bug in the original export.
  The import decodes such escapes and then drops all Unicode `Cf`
  characters, which makes the two sides agree.

Objects are stored under an ASCII key derived from the word id,
`{type}/{id}{ext}`, not the original filename: S3 request signing and Cyrillic
keys are a bad combination, and four parasite filenames are shared by more
than one word (`әйтеуір.mp3` by three), so filenames are not unique anyway.
The admin page uses the same scheme for clips it uploads.

Words whose clip is missing keep `audio_key = NULL`; their `/audio/{id}`
returns 404 and everything else about them works. The import logs each one.

Every seeded word has a clip. The original audio archive had one under a
misspelled name: `ежелгі дауир.mp3` for word 5939 `Ежелгі дәуір`. Copies of
the clips taken from that archive need it renamed to `ежелгі дәуір.mp3`; the
import does not fuzzy-match Cyrillic, so otherwise that word gets no clip.

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
  `[offset*limit : (offset+1)*limit]` and the clients' paging increments it
  by one per page, so this is load-bearing; it is deliberately preserved.
- `limit` — page size, capped at `max_page_size` (100)
- `sort` — `asc` or `desc`, by id

`correctVersions` is **omitted entirely** for commonly-mispronounced words
rather than sent as `[]`, and the `incorrectUsage` / `correctUsage` keys are
omitted when a correct version has no usage example. Both match the 2023
responses exactly, which the clients depend on.

Audio is proxied through the backend rather than served as a presigned
redirect, because a presigned URL would point at the RustFS endpoint, which
is not reachable from outside the compose network.

## Admin page

`/admin/` — one password (`ADMIN_PASSWORD`), no user table. The session is a
signed cookie keyed by `SECRET_KEY`; changing `SECRET_KEY` logs everyone out.

**The interface is Kazakh only** — there is no language switch and no
fallback locale. The two type names are copied verbatim from
the Telegram bot, so the admin page and the bot name the same thing the same
way.

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

New words get ids from `word_id_seq`, which starts at 1 000 000 so they can
never collide with the seeded ids (0–22 193).

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
`app/src/duryssoile/admin/labels.py` — that one set drives both the form and the
server-side rule.

### The rest

Create and edit words, manage correct versions, and upload or delete a clip
(mp3/wav/ogg/opus/m4a/aac, 10 MB max) which goes straight into RustFS.
Deleting a word cascades to its correct versions and removes its object.

The password is compared with `secrets.compare_digest`, and five failed
attempts from one address trigger a 60-second lockout. The address is the
real client's, taken from the `X-Forwarded-For` header Caddy sets (uvicorn
runs with `--forwarded-allow-ips`). The throttle is per-process, so it is a
speed bump rather than a real rate limiter; consider restricting the admin
page to the internal network too.

## Notes

- `SECRET_KEY`, `ADMIN_PASSWORD` and the RustFS keys are required; the app
  refuses to start without them instead of serving empty results.
- The backend checks Postgres and creates the RustFS bucket on startup, so a
  broken deployment fails loudly rather than returning `[]`.
- `/health` checks the database round trip and backs the compose healthcheck.
