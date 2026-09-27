-- Run after bulk load.

CREATE INDEX ON prices (code, code_type, payer_key);
CREATE INDEX ON prices (payer_key, plan_key);
CREATE INDEX ON prices (org_key) WHERE org_key IS NOT NULL;
CREATE INDEX ON prices (rate_set_id) WHERE rate_set_id IS NOT NULL;
CREATE INDEX ON prices (item_id) WHERE item_id IS NOT NULL;

CREATE INDEX ON rate_set_providers (rate_set_id);
CREATE INDEX ON rate_set_providers (provider_group_id);
CREATE INDEX ON provider_group_npis (npi);
CREATE INDEX ON provider_group_npis (provider_group_id);
CREATE INDEX ON provider_groups (tin);
CREATE INDEX ON provider_groups (org_key);
CREATE INDEX ON org_npis (npi);
CREATE INDEX ON organizations (tin);

CREATE INDEX ON item_codes (code, code_type);
CREATE INDEX ON item_codes (item_id);

-- Fuzzy search for the resolve tool.
CREATE INDEX ON hospital_items USING gin (description gin_trgm_ops);
CREATE INDEX ON billing_codes USING gin (description gin_trgm_ops);
CREATE INDEX ON provider_groups USING gin (business_name gin_trgm_ops);
CREATE INDEX ON organizations USING gin (name gin_trgm_ops);
CREATE INDEX ON payer_aliases USING gin (alias gin_trgm_ops);
CREATE INDEX ON org_aliases USING gin (alias gin_trgm_ops);

-- Link MRF provider groups to known organizations by TIN.
UPDATE provider_groups pg SET org_key = o.org_key
FROM organizations o WHERE o.tin IS NOT NULL AND pg.tin = o.tin;

ANALYZE;

-- Which sources carry each plan (the mediator uses this to tell hospital-file plans from payer-file
-- networks without scanning prices).
DROP TABLE IF EXISTS plan_sources;
CREATE TABLE plan_sources AS
SELECT DISTINCT p.payer_key, p.plan_key, p.source_id, s.source_type
FROM prices p JOIN sources s USING (source_id)
WHERE p.plan_key IS NOT NULL;
