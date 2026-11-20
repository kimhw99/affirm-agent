-- case_db: the case service's tables.
--
-- The case service owns this database (account case_svc, created by db/init.sql) and
-- runs this file when it starts. Every statement uses IF NOT EXISTS, so running it
-- again changes nothing. After changing a table, drop the database (or its schemas)
-- and load the golden cases again.
--
-- Values are checked by the kyb_contracts models, not here: the seed script loads only
-- golden cases that pass scripts/validate_golden.py, and step 11 saves a validated
-- DecisionRecord. This schema adds keys, references and NOT NULL, plus a check on the
-- case status, the one value the case service changes itself.
--
-- Two schemas:
--   public   the cases, served as CaseInput (GET /cases/{case_id} and endpoints 7-10),
--            and the decisions and audit log that step 11 writes
--   mock     the mocked registry, DBA, commercial and IRS records from world.json
--            (endpoints 1-6)
--
-- No table holds a full TIN or SSN. Like CaseInput and world.json, the tables carry
-- only a TIN's type and last 4 digits, and owners appear as owner:P1. The full numbers
-- stay with the existing KYB system.
--
-- The endpoint numbers are those in docs/case-service-endpoints.md.

CREATE SCHEMA IF NOT EXISTS mock;


-- ---------- Cases (case.json, served as CaseInput) ----------

CREATE TABLE IF NOT EXISTS cases (
    case_id          text PRIMARY KEY,                      -- case_1042
    status           text NOT NULL CHECK (status IN
                         ('pending_review', 'approved', 'docs_requested', 'escalated', 'denied')),
    flag_reasons     text[] NOT NULL,
    -- application
    legal_name       text NOT NULL,
    dba              text,
    address          text NOT NULL,
    tin_type         text,                                  -- EIN or SSN; null when no TIN was submitted
    tin_last4        text,
    state            text NOT NULL,
    state_entity_id  text,
    entity_type      text NOT NULL,
    formation_date   date,
    -- the vendor's checks, exactly as CaseInput.checks
    checks           jsonb NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cases_status_idx ON cases (status);    -- the review queue filters by status

CREATE TABLE IF NOT EXISTS case_owners (                    -- application.owners
    case_id    text NOT NULL REFERENCES cases ON DELETE CASCADE,
    owner_ref  text NOT NULL,                               -- owner:P1
    name       text NOT NULL,
    role       text NOT NULL,
    PRIMARY KEY (case_id, owner_ref)
);

-- Evidence IDs (D1, B1, W1, K1) are unique only within a case, so each key starts with case_id.

CREATE TABLE IF NOT EXISTS documents (                      -- endpoint 8
    case_id     text NOT NULL REFERENCES cases ON DELETE CASCADE,
    doc_id      text NOT NULL,                              -- D1
    type        text NOT NULL,
    extracted   jsonb NOT NULL DEFAULT '{}',                -- keys per type, as in DOCUMENT_FIELDS
    confidence  double precision NOT NULL,
    raw_text    text,                                       -- full OCR text; never returned, nothing fills it yet
    PRIMARY KEY (case_id, doc_id)
);

CREATE TABLE IF NOT EXISTS bank_verifications (             -- endpoint 9
    case_id         text NOT NULL REFERENCES cases ON DELETE CASCADE,
    bank_id         text NOT NULL,                          -- B1
    account_holder  text NOT NULL,
    status          text NOT NULL,
    verified_at     date,
    PRIMARY KEY (case_id, bank_id),
    UNIQUE (case_id)                                        -- a case has at most one
);

CREATE TABLE IF NOT EXISTS web_snapshots (                  -- endpoint 10
    case_id      text NOT NULL REFERENCES cases ON DELETE CASCADE,
    web_id       text NOT NULL,                             -- W1
    url          text NOT NULL,
    captured_at  date NOT NULL,
    extracted    jsonb NOT NULL DEFAULT '{}',               -- keys in WEB_FIELDS
    page_text    text,                                      -- full page text; never returned, nothing fills it yet
    PRIMARY KEY (case_id, web_id)
);

CREATE TABLE IF NOT EXISTS owner_kyc (                      -- endpoint 7
    case_id    text NOT NULL,
    kyc_id     text NOT NULL,                               -- K1
    owner_ref  text NOT NULL,
    result     text NOT NULL,
    PRIMARY KEY (case_id, kyc_id),
    FOREIGN KEY (case_id, owner_ref) REFERENCES case_owners ON DELETE CASCADE
);


-- ---------- Step 11: decisions and the audit log ----------
-- No ON DELETE CASCADE here: deleting a case that already has decisions fails instead
-- of erasing its history. A full reload empties them with TRUNCATE cases CASCADE.

CREATE TABLE IF NOT EXISTS decisions (                      -- DecisionRecord (decision.py)
    review_id             text PRIMARY KEY,
    case_id               text NOT NULL REFERENCES cases,
    decision              text NOT NULL,
    note                  text NOT NULL DEFAULT '',
    analyst_id            text NOT NULL,
    agent_recommendation  text NOT NULL,
    overridden            boolean NOT NULL,
    decided_at            timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_case_idx ON decisions (case_id);

CREATE TABLE IF NOT EXISTS audit_log (                      -- who changed a case's status, from what to what, and when
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id      text NOT NULL REFERENCES cases,
    review_id    text,
    actor_id     text NOT NULL,                             -- analyst_id from the JWT
    action       text NOT NULL,                             -- e.g. decision
    from_status  text,
    to_status    text,
    detail       jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_case_idx ON audit_log (case_id);


-- ---------- mock: the mocked registry, DBA, commercial and IRS records (world.json) ----------
-- Every golden case's world.json goes into these same tables. source_case records which
-- case a row came from (never returned), so reloading one case replaces only its rows.
-- The IRS record belongs to one case, so mock.tin_records is keyed by case_id instead.

CREATE TABLE IF NOT EXISTS mock.registry_entities (         -- endpoints 1 and 2
    state               text NOT NULL,
    state_entity_id     text NOT NULL,
    legal_name          text NOT NULL,
    former_names        text[] NOT NULL DEFAULT '{}',
    entity_type         text NOT NULL,
    entity_status       text NOT NULL,
    formation_date      date,
    registered_address  text NOT NULL,
    officers            jsonb NOT NULL DEFAULT '[]',        -- [{"name": ..., "title": ...}], as endpoint 2 returns it
    source_case         text NOT NULL REFERENCES cases ON DELETE CASCADE,
    PRIMARY KEY (state, state_entity_id)                    -- entity IDs are unique only within a state
);

CREATE TABLE IF NOT EXISTS mock.registry_filings (          -- endpoint 3
    event_id         text PRIMARY KEY,
    state            text NOT NULL,
    state_entity_id  text NOT NULL,
    event_type       text NOT NULL,
    effective_date   date NOT NULL,
    old_value        text,
    new_value        text,
    FOREIGN KEY (state, state_entity_id) REFERENCES mock.registry_entities ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS registry_filings_entity_idx
    ON mock.registry_filings (state, state_entity_id, effective_date);    -- oldest first

CREATE TABLE IF NOT EXISTS mock.dba_filings (               -- endpoint 4
    filing_id                   text PRIMARY KEY,
    state                       text NOT NULL,
    county                      text,                       -- null for a state-level filing
    dba                         text NOT NULL,
    registrant                  text NOT NULL,
    registrant_type             text NOT NULL,
    registrant_state_entity_id  text,
    filed                       date NOT NULL,
    expires                     date,
    status                      text NOT NULL,
    source_case                 text NOT NULL REFERENCES cases ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mock.commercial_profiles (       -- endpoint 5
    profile_id     text PRIMARY KEY,
    state          text NOT NULL,                           -- endpoint 5 filters on it but doesn't return it
    business_name  text NOT NULL,
    address        text NOT NULL,
    first_seen     date NOT NULL,
    last_updated   date NOT NULL,
    source_case    text NOT NULL REFERENCES cases ON DELETE CASCADE
);

-- world.json's tin_record: what the IRS has on file for the TIN the merchant submitted,
-- as endpoint 6 returns it. tin_type and tin_last4 repeat the case's own
-- (validate_golden.py checks that they agree), so endpoint 6 reads only this table.
-- A TIN the IRS doesn't know is a row with irs_status not_found. A case with no TIN
-- has no row, and endpoint 6 returns 404.
CREATE TABLE IF NOT EXISTS mock.tin_records (               -- endpoint 6
    case_id     text PRIMARY KEY REFERENCES cases ON DELETE CASCADE,
    tin_type    text NOT NULL,
    tin_last4   text NOT NULL,
    irs_status  text NOT NULL,                              -- issued, not_issued or not_found
    irs_name    text                                        -- null unless issued
);
