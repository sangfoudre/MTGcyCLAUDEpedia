-- MTGcyclopedia — schéma SQLite
-- Bitmask couleurs : W=1 U=2 B=4 R=8 G=16

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---------------------------------------------------------------- sets
CREATE TABLE IF NOT EXISTS sets (
    id              TEXT PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,
    mtgo_code       TEXT,
    arena_code      TEXT,
    name            TEXT NOT NULL,
    set_type        TEXT,
    released_at     TEXT,
    released_year   INTEGER,
    block_code      TEXT,
    block           TEXT,
    parent_set_code TEXT,
    card_count      INTEGER,
    printed_size    INTEGER,
    digital         INTEGER DEFAULT 0,
    foil_only       INTEGER DEFAULT 0,
    nonfoil_only    INTEGER DEFAULT 0,
    icon_svg_uri    TEXT,
    icon_path       TEXT,
    scryfall_uri    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sets_released ON sets(released_at);
CREATE INDEX IF NOT EXISTS idx_sets_type     ON sets(set_type);

-- ---------------------------------------------------------------- cards
CREATE TABLE IF NOT EXISTS cards (
    id                 TEXT PRIMARY KEY,
    oracle_id          TEXT,
    name               TEXT NOT NULL,
    lang               TEXT,
    layout             TEXT,

    set_code           TEXT,
    set_id             TEXT,
    collector_number   TEXT,
    cn_num             INTEGER,   -- partie numérique, pour le tri naturel
    cn_suffix          TEXT,      -- suffixe (a, b, ★, †, …)
    cn_prefix          TEXT,      -- préfixe alphabétique éventuel

    rarity             TEXT,
    rarity_rank        INTEGER,   -- common=1 … bonus=6, pour r>=rare
    released_at        TEXT,
    released_year      INTEGER,

    mana_cost          TEXT,
    cmc                REAL,
    type_line          TEXT,
    oracle_text        TEXT,
    oracle_plain       TEXT,   -- oracle sans texte de rappel (pour o:)
    flavor_text        TEXT,

    power              TEXT,
    toughness          TEXT,
    loyalty            TEXT,
    defense            TEXT,
    pow_num            REAL,      -- NULL si non numérique (*, 1+*, …)
    tou_num            REAL,
    loy_num            REAL,

    colors             INTEGER DEFAULT 0,
    color_identity     INTEGER DEFAULT 0,
    color_count        INTEGER DEFAULT 0,
    ci_count           INTEGER DEFAULT 0,
    produced_mana      INTEGER DEFAULT 0,

    artist             TEXT,
    artist_count       INTEGER DEFAULT 0,
    illustration_id    TEXT,

    border_color       TEXT,
    frame              TEXT,
    security_stamp     TEXT,
    watermark          TEXT,

    image_status       TEXT,
    image_updated_at   TEXT,
    highres_image      INTEGER DEFAULT 0,

    full_art           INTEGER DEFAULT 0,
    textless           INTEGER DEFAULT 0,
    digital            INTEGER DEFAULT 0,
    promo              INTEGER DEFAULT 0,
    reprint            INTEGER DEFAULT 0,
    variation          INTEGER DEFAULT 0,
    variation_of       TEXT,
    reserved           INTEGER DEFAULT 0,
    booster            INTEGER DEFAULT 0,
    story_spotlight    INTEGER DEFAULT 0,
    oversized          INTEGER DEFAULT 0,
    game_changer       INTEGER DEFAULT 0,
    content_warning    INTEGER DEFAULT 0,

    face_count         INTEGER DEFAULT 0,
    is_unique_art      INTEGER DEFAULT 0,  -- représentant de son illustration_id
    edhrec_rank        INTEGER,
    penny_rank         INTEGER,

    scryfall_uri       TEXT,

    -- colonnes JSON (SQLite JSON1) pour le froid
    image_uris_json    TEXT,
    prices_json        TEXT,
    legalities_json    TEXT,
    finishes_json      TEXT,
    games_json         TEXT,
    keywords_json      TEXT,
    promo_types_json   TEXT,
    frame_effects_json TEXT,
    multiverse_ids_json TEXT,
    all_parts_json     TEXT,

    FOREIGN KEY (set_code) REFERENCES sets(code)
);
CREATE INDEX IF NOT EXISTS idx_cards_oracle   ON cards(oracle_id);
CREATE INDEX IF NOT EXISTS idx_cards_set      ON cards(set_code, cn_num, cn_suffix);
CREATE INDEX IF NOT EXISTS idx_cards_illus    ON cards(illustration_id);
CREATE INDEX IF NOT EXISTS idx_cards_colors   ON cards(colors);
CREATE INDEX IF NOT EXISTS idx_cards_ci       ON cards(color_identity);
CREATE INDEX IF NOT EXISTS idx_cards_cmc      ON cards(cmc);
CREATE INDEX IF NOT EXISTS idx_cards_rarity   ON cards(rarity_rank);
CREATE INDEX IF NOT EXISTS idx_cards_lang     ON cards(lang);
CREATE INDEX IF NOT EXISTS idx_cards_artist   ON cards(artist);
CREATE INDEX IF NOT EXISTS idx_cards_uniqart  ON cards(is_unique_art);
CREATE INDEX IF NOT EXISTS idx_cards_name     ON cards(name);

-- ---------------------------------------------------------------- faces
CREATE TABLE IF NOT EXISTS card_faces (
    card_id         TEXT NOT NULL,
    face_index      INTEGER NOT NULL,
    name            TEXT,
    mana_cost       TEXT,
    type_line       TEXT,
    oracle_text     TEXT,
    oracle_plain    TEXT,
    flavor_text     TEXT,
    power           TEXT,
    toughness       TEXT,
    loyalty         TEXT,
    defense         TEXT,
    pow_num         REAL,
    tou_num         REAL,
    loy_num         REAL,
    colors          INTEGER DEFAULT 0,
    artist          TEXT,
    illustration_id TEXT,
    watermark       TEXT,
    image_uris_json TEXT,
    PRIMARY KEY (card_id, face_index),
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_faces_illus ON card_faces(illustration_id);

-- ---------------------------------------------------------------- artistes
CREATE TABLE IF NOT EXISTS artists (
    id   TEXT PRIMARY KEY,
    name TEXT
);
CREATE INDEX IF NOT EXISTS idx_artists_name ON artists(name);

CREATE TABLE IF NOT EXISTS card_artists (
    card_id   TEXT NOT NULL,
    artist_id TEXT NOT NULL,
    ord       INTEGER DEFAULT 0,
    PRIMARY KEY (card_id, artist_id),
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cardartists_artist ON card_artists(artist_id);

-- ---------------------------------------------------------------- légalités
CREATE TABLE IF NOT EXISTS legalities (
    card_id TEXT NOT NULL,
    format  TEXT NOT NULL,
    status  TEXT NOT NULL,
    PRIMARY KEY (card_id, format),
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_legal_fmt ON legalities(format, status);

-- ---------------------------------------------------------------- rulings
CREATE TABLE IF NOT EXISTS rulings (
    oracle_id    TEXT NOT NULL,
    source       TEXT,
    published_at TEXT,
    comment      TEXT
);
CREATE INDEX IF NOT EXISTS idx_rulings_oid ON rulings(oracle_id);

-- ---------------------------------------------------------------- tags (Tagger)
CREATE TABLE IF NOT EXISTS tags (
    id    TEXT PRIMARY KEY,
    label TEXT,
    slug  TEXT,
    type  TEXT          -- 'illustration' | 'oracle'
);
CREATE INDEX IF NOT EXISTS idx_tags_slug ON tags(slug);

CREATE TABLE IF NOT EXISTS art_taggings (
    tag_id          TEXT NOT NULL,
    illustration_id TEXT NOT NULL,
    weight          TEXT,
    PRIMARY KEY (tag_id, illustration_id)
);
CREATE INDEX IF NOT EXISTS idx_arttag_illus ON art_taggings(illustration_id);

CREATE TABLE IF NOT EXISTS oracle_taggings (
    tag_id    TEXT NOT NULL,
    oracle_id TEXT NOT NULL,
    weight    TEXT,
    PRIMARY KEY (tag_id, oracle_id)
);
CREATE INDEX IF NOT EXISTS idx_oracletag_oid ON oracle_taggings(oracle_id);

-- ---------------------------------------------------------------- images
CREATE TABLE IF NOT EXISTS images (
    card_id      TEXT NOT NULL,
    face_index   INTEGER NOT NULL DEFAULT 0,
    fmt          TEXT NOT NULL,      -- png / large / normal / art_crop / …
    path         TEXT NOT NULL,
    bytes        INTEGER,
    src_updated  TEXT,               -- image_updated_at au moment du DL
    downloaded_at TEXT,
    PRIMARY KEY (card_id, face_index, fmt),
    FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_images_path ON images(path);

-- ---------------------------------------------------------------- FTS5
-- Table externe (contentless-external) alimentée par 'rebuild'.
CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5(
    name, oracle_text, oracle_plain, flavor_text, type_line, artist,
    content='cards', content_rowid='rowid',
    tokenize='trigram remove_diacritics 1'
);
