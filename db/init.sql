-- Creates the three databases and their accounts.
--
-- PostgreSQL runs this once, the first time the database container starts with an
-- empty data folder (mount it in /docker-entrypoint-initdb.d/). Each service then
-- creates its own tables in its own database:
--   auth_db   auth service: users and roles
--   case_db   case service: services/case/schema.sql
--   agent_db  agent service: LangGraph's checkpoint tables (PostgresSaver.setup())
--
-- The passwords come from the environment (.env; see .env.example), never from this
-- file. To run it by hand as a superuser against a local server:
--   AUTH_DB_PASSWORD=... CASE_DB_PASSWORD=... AGENT_DB_PASSWORD=... psql -d postgres -f db/init.sql
-- \getenv needs psql 15 or later; the postgres:16 image has it.

\set ON_ERROR_STOP on

\getenv auth_password AUTH_DB_PASSWORD
\getenv case_password CASE_DB_PASSWORD
\getenv agent_password AGENT_DB_PASSWORD

-- Stop with a clear message when a password is missing.
\if :{?auth_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'AUTH_DB_PASSWORD is not set; see .env.example'; END $$;
\endif
\if :{?case_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'CASE_DB_PASSWORD is not set; see .env.example'; END $$;
\endif
\if :{?agent_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'AGENT_DB_PASSWORD is not set; see .env.example'; END $$;
\endif

CREATE ROLE auth_svc LOGIN PASSWORD :'auth_password';
CREATE ROLE case_svc LOGIN PASSWORD :'case_password';
CREATE ROLE agent_svc LOGIN PASSWORD :'agent_password';

CREATE DATABASE auth_db OWNER auth_svc;
CREATE DATABASE case_db OWNER case_svc;
CREATE DATABASE agent_db OWNER agent_svc;

-- Each database admits only its own account (and the superuser).
REVOKE ALL ON DATABASE auth_db, case_db, agent_db FROM PUBLIC;
