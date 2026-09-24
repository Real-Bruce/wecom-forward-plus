-- Schema for the bundled PostgreSQL container, mounted into
-- /docker-entrypoint-initdb.d/ so the `groups` table exists as soon as the
-- database is ready. Kept in sync with DDL in src/db_config.py; the
-- application also runs `CREATE TABLE IF NOT EXISTS` on startup, so this
-- script is an idempotent early bootstrap, not the schema owner.
--
-- Only executed when the pgdata volume is first initialized.

CREATE TABLE IF NOT EXISTS groups (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    wecom_robot_id      TEXT NOT NULL,
    wecom_robot_secret  TEXT NOT NULL,
    dify_api_key        TEXT NOT NULL,
    session_max_total   INTEGER,
    session_ttl_seconds INTEGER,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
