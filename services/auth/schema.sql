-- auth_db: the auth service's tables.
--
-- The auth service owns this database (account auth_svc, created by db/init.sql) and
-- runs this file when it starts. Every statement uses IF NOT EXISTS, so running it
-- again changes nothing. The roles are those in contracts/kyb_contracts/auth.py.

CREATE TABLE IF NOT EXISTS users (
    user_id        text PRIMARY KEY,                        -- u_analyst_01; analyst_id in DecisionRecord
    username       text NOT NULL UNIQUE,
    display_name   text NOT NULL,
    role           text NOT NULL CHECK (role IN ('analyst', 'senior_analyst')),
    password_hash  text NOT NULL,                           -- scrypt; never returned
    created_at     timestamptz NOT NULL DEFAULT now()
);
