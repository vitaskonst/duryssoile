-- «Дұрыс сөйле» schema.
--
-- Design note: refactor_v1 split words into `parasite_word` and
-- `commonly_mispronounced_word`, which destroyed the global id space the
-- public API and the Telegram bot rely on (/words/{id}, /audio/{id}).
-- The original JSON data uses ONE id space across both types
-- (parasite 0-185, commonly-mispronounced 186-22193), so we keep a single
-- `word` table with a `type` discriminator. Ids stay stable across reseeds.

DROP TABLE IF EXISTS correct_version CASCADE;
DROP TABLE IF EXISTS word CASCADE;
DROP TYPE IF EXISTS word_type CASCADE;

CREATE TYPE word_type AS ENUM ('parasite', 'commonly-mispronounced');

CREATE TABLE word (
    id         INTEGER PRIMARY KEY,
    type       word_type NOT NULL,
    word       TEXT      NOT NULL CHECK (word <> ''),
    -- Object key inside the RustFS bucket, e.g. 'parasite/тәбет.mp3'.
    -- NULL means "no audio uploaded yet" -> /audio/{id} returns 404.
    audio_key  TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Admin creates rows without choosing an id. The sequence starts at 1000000
-- so admin ids can never collide with seeded ids: the JSON owns [0, 1000000)
-- and may grow into it freely, while the admin page owns [1000000, inf).
-- init.py refuses a data file that reaches into the admin range.
CREATE SEQUENCE word_id_seq START WITH 1000000 MINVALUE 1 OWNED BY word.id;
ALTER TABLE word ALTER COLUMN id SET DEFAULT nextval('word_id_seq');

CREATE INDEX word_type_id_idx ON word (type, id);
-- Supports the API's case-insensitive prefix filter.
CREATE INDEX word_type_lower_word_idx ON word (type, lower(word) text_pattern_ops);

CREATE TABLE correct_version (
    id              SERIAL PRIMARY KEY,
    word_id         INTEGER NOT NULL REFERENCES word (id) ON DELETE CASCADE,
    word            TEXT    NOT NULL CHECK (word <> ''),
    -- Both usage examples are absent for 16 of the 216 seeded rows.
    incorrect_usage TEXT,
    correct_usage   TEXT,
    -- Preserves the order the versions appeared in the source JSON, so API
    -- responses are byte-identical to the original across reseeds.
    position        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX correct_version_word_id_position_idx
    ON correct_version (word_id, position);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER word_touch_updated_at
    BEFORE UPDATE ON word
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
