-- ClarityBill schema. Every source (hospital standard-charge files, payer TiC MRFs, CMS fee
-- schedule) lands in the same tables. Nothing is filtered at load time: filtering happens in the
-- mediator's queries. Big tables have no FKs/indexes during load; run indexes.sql afterwards.
--
-- How a bill line gets pinned to a rate:
--   bill hospital (NPI / TIN / name) -> organizations -> provider_groups (MRF) or prices.org_key (hospital file)
--   bill payer / plan name           -> payers / plans (via payer_aliases)
--   bill CPT/HCPCS/DRG + modifiers    -> prices.code / code_type / modifiers
--   bill type (hospital vs clinic)   -> prices.billing_class

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- One row per ingested file.
CREATE TABLE sources (
    source_id       TEXT PRIMARY KEY,   -- e.g. 'uw_sc_2026_04', 'wps_mrf_020'
    source_type     TEXT NOT NULL,      -- 'hospital_standard_charges' | 'payer_mrf' | 'cms_pfs'
    publisher       TEXT,               -- who published the file (hospital or payer name)
    file_name       TEXT,
    last_updated_on TEXT,
    loaded_at       TIMESTAMPTZ DEFAULT now()
);

-- ---------- Who pays ----------

CREATE TABLE payers (
    payer_key TEXT PRIMARY KEY,         -- normalized: lowercase, '_' -> ' ' (e.g. 'wps', 'united healthcare')
    name      TEXT NOT NULL             -- as first seen in a source
);

CREATE TABLE plans (
    plan_key  TEXT PRIMARY KEY,         -- payer_key || '|' || normalized plan/network name
    payer_key TEXT NOT NULL REFERENCES payers,
    name      TEXT NOT NULL             -- hospital file: plan column name; MRF: network_name
);

-- Hand-maintained spellings seen on bills / insurance cards -> payer (and optionally plan).
CREATE TABLE payer_aliases (
    alias     TEXT PRIMARY KEY,         -- lowercase
    payer_key TEXT NOT NULL REFERENCES payers,
    plan_key  TEXT REFERENCES plans
);

-- ---------- Who provides ----------

-- Real-world billing entities (a hospital, a physician group). TIN is the join key to MRF data.
CREATE TABLE organizations (
    org_key     TEXT PRIMARY KEY,       -- e.g. 'uwhc', 'uwmf'
    name        TEXT NOT NULL,
    org_type    TEXT NOT NULL,          -- 'hospital' | 'physician_group' | 'other'
    system_name TEXT,                   -- umbrella brand printed on bills, e.g. 'UW Health'
    tin         TEXT,
    address     TEXT
);

CREATE TABLE org_npis (
    org_key TEXT NOT NULL REFERENCES organizations,
    npi     TEXT NOT NULL,
    PRIMARY KEY (org_key, npi)
);

-- Hand-maintained names seen on bills -> organization.
CREATE TABLE org_aliases (
    alias   TEXT PRIMARY KEY,           -- lowercase
    org_key TEXT NOT NULL REFERENCES organizations
);

-- MRF provider groups (from provider_references, or synthesized from inline provider_groups).
CREATE TABLE provider_groups (
    provider_group_id BIGINT PRIMARY KEY,   -- globally unique (source offset + file id)
    source_id         TEXT NOT NULL,
    file_group_id     TEXT,                 -- provider_group_id as written in the file, if any
    network_name      TEXT,
    tin_type          TEXT,                 -- 'ein' | 'npi'
    tin               TEXT,
    business_name     TEXT,
    org_key           TEXT                  -- filled post-load by TIN match (see indexes.sql)
);

CREATE TABLE provider_group_npis (
    provider_group_id BIGINT NOT NULL,
    npi               TEXT NOT NULL
);

-- ---------- What was done ----------

CREATE TABLE billing_codes (
    code        TEXT NOT NULL,          -- normalized: CPT/HCPCS upper-case, DRGs zero-padded to 3
    code_type   TEXT NOT NULL,          -- 'CPT' | 'HCPCS' | 'MS-DRG' | 'APR-DRG' | 'EAPG' | 'RC' | 'NDC' | 'LOCAL'
    description TEXT,
    PRIMARY KEY (code, code_type)
);

-- One row per line of a hospital standard-charges file (a chargemaster item).
-- description is the hospital's own chargemaster text, which is what its bills print.
CREATE TABLE hospital_items (
    item_id          BIGINT PRIMARY KEY,
    source_id        TEXT NOT NULL,
    org_key          TEXT NOT NULL,
    cdm_code         TEXT,              -- hospital's internal chargemaster code
    description      TEXT,
    setting          TEXT,              -- 'inpatient' | 'outpatient' | 'both'
    modifiers        TEXT[],
    gross_charge     NUMERIC(14,4),     -- list price before any discount (what an uninsured bill shows)
    discounted_cash  NUMERIC(14,4),     -- self-pay price
    min_negotiated   NUMERIC(14,4),     -- de-identified min across all payers
    max_negotiated   NUMERIC(14,4),
    drug_unit        NUMERIC(14,4),
    drug_unit_type   TEXT,
    generic_notes    TEXT
);

-- All codes attached to a hospital item (CPT/HCPCS, revenue code, NDC, DRG, ...).
CREATE TABLE item_codes (
    item_id        BIGINT NOT NULL,
    code           TEXT NOT NULL,
    code_type      TEXT NOT NULL,       -- normalized type
    raw_code_type  TEXT                 -- as written in the file
);

-- MRF: one negotiated_rates[] entry = a set of providers sharing a list of prices for one code.
CREATE TABLE rate_sets (
    rate_set_id BIGINT PRIMARY KEY,
    source_id   TEXT NOT NULL
);

CREATE TABLE rate_set_providers (
    rate_set_id       BIGINT NOT NULL,
    provider_group_id BIGINT NOT NULL
);

-- ---------- The rates ----------
-- Every negotiated price from every source. Read rate_kind before using any number:
--   'dollar'             negotiated_dollar is the full allowed amount for the service
--   'percent_of_charges' negotiated_percentage is a % of billed charges; negotiated_dollar is only
--                        filled when the source computed it (hospital files do: gross x %)
--   'per_diem'           negotiated_dollar is per day, not per stay
--   'per_unit'           negotiated_dollar is a multiplier (e.g. anesthesia $ per ASA unit)
--   'algorithm'          no number; see negotiated_algorithm / notes
CREATE TABLE prices (
    price_id              BIGSERIAL PRIMARY KEY,
    source_id             TEXT NOT NULL,
    code                  TEXT NOT NULL,
    code_type             TEXT NOT NULL,
    modifiers             TEXT[],           -- NULL/empty = base rate, no modifier
    payer_key             TEXT NOT NULL,
    plan_key              TEXT,
    org_key               TEXT,             -- set for hospital-file rows (the hospital is the provider)
    item_id               BIGINT,           -- hospital-file row this came from
    rate_set_id           BIGINT,           -- MRF: which provider groups this applies to
    billing_class         TEXT,             -- 'professional' | 'institutional'
    setting               TEXT,             -- 'inpatient' | 'outpatient' | 'both'
    rate_kind             TEXT NOT NULL,
    methodology           TEXT,             -- raw: 'fee schedule', 'case rate', 'percent of total billed charges', ...
    negotiated_dollar     NUMERIC(14,4),
    negotiated_percentage NUMERIC(9,4),
    negotiated_algorithm  TEXT,
    median_paid           NUMERIC(14,4),    -- hospital files: allowed amounts actually paid (v3.0)
    p10_paid              NUMERIC(14,4),
    p90_paid              NUMERIC(14,4),
    paid_count            TEXT,             -- '0', '1 through 10', or a number
    service_codes         TEXT[],           -- CMS place-of-service codes (MRF)
    expiration_date       DATE,
    notes                 TEXT
);
