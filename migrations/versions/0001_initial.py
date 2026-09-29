"""Initial schema.

The schema the pre-Alembic `db/schema.sql` created, reproduced exactly so a
database built by it can be adopted by stamping this revision (app.bootstrap
does that automatically).

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The public API addresses words by a single global id (/words/{id},
    # /audio/{id}), so both types share one table with a `type` discriminator
    # rather than a table each.
    op.execute("CREATE TYPE word_type AS ENUM ('parasite', 'commonly-mispronounced')")

    op.execute("""
        CREATE TABLE word (
            id         INTEGER PRIMARY KEY,
            type       word_type NOT NULL,
            word       TEXT      NOT NULL CONSTRAINT word_word_check CHECK (word <> ''),
            -- Object key inside the RustFS bucket, e.g. 'parasite/42.mp3'.
            -- NULL means "no audio" -> /audio/{id} returns 404.
            audio_key  TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # Seeded ids come from the JSON; words created through the admin page draw
    # from this sequence, which starts well clear of them.
    op.execute('CREATE SEQUENCE word_id_seq START WITH 1000000 MINVALUE 1 OWNED BY word.id')
    op.execute("ALTER TABLE word ALTER COLUMN id SET DEFAULT nextval('word_id_seq')")

    op.execute('CREATE INDEX word_type_id_idx ON word (type, id)')
    # Supports the API's case-insensitive prefix filter.
    op.execute(
        'CREATE INDEX word_type_lower_word_idx ON word (type, lower(word) text_pattern_ops)'
    )

    op.execute("""
        CREATE TABLE correct_version (
            id              SERIAL PRIMARY KEY,
            word_id         INTEGER NOT NULL REFERENCES word (id) ON DELETE CASCADE,
            word            TEXT    NOT NULL
                            CONSTRAINT correct_version_word_check CHECK (word <> ''),
            incorrect_usage TEXT,
            correct_usage   TEXT,
            -- Keeps the order the versions appeared in the source JSON.
            position        INTEGER NOT NULL DEFAULT 0
        )
    """)
    op.execute(
        'CREATE INDEX correct_version_word_id_position_idx '
        'ON correct_version (word_id, position)'
    )

    # Unindented: Postgres stores a function body verbatim.
    op.execute("""
CREATE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
""")
    op.execute("""
        CREATE TRIGGER word_touch_updated_at
            BEFORE UPDATE ON word
            FOR EACH ROW EXECUTE FUNCTION touch_updated_at()
    """)


def downgrade() -> None:
    op.execute('DROP TABLE correct_version')
    op.execute('DROP TABLE word')
    op.execute('DROP FUNCTION touch_updated_at()')
    op.execute('DROP TYPE word_type')
