#!/bin/bash
# Load parser output (gzipped CSVs) into Postgres.
#
#   db/load.sh --fresh parsers/out/uw_sc_2026_04 parsers/out/wps_mrf_020
#
# --fresh drops and recreates the schema first. seed.sql and indexes.sql run after the data.
set -euo pipefail
cd "$(dirname "$0")/.."
DB=${DATABASE_URL:-postgresql://postgres@localhost:5432/claritybill}
PSQL=(psql "$DB" -v ON_ERROR_STOP=1 -q)

if [[ "${1:-}" == "--fresh" ]]; then
  shift
  "${PSQL[@]}" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
  "${PSQL[@]}" -f db/schema.sql
fi

# Small dimension tables are shared across sources: insert with ON CONFLICT DO NOTHING.
DEDUPE=" sources payers plans billing_codes "
ORDER="sources payers plans billing_codes provider_groups provider_group_npis hospital_items item_codes rate_sets rate_set_providers prices"

for dir in "$@"; do
  for t in $ORDER; do
    f="$dir/$t.csv.gz"
    [[ -f "$f" ]] || continue
    cols=$(gzip -dc "$f" | head -1 || true)
    echo "loading $f"
    if [[ "$DEDUPE" == *" $t "* ]]; then
      "${PSQL[@]}" <<SQL
CREATE TEMP TABLE stg AS SELECT $cols FROM $t LIMIT 0;
\copy stg ($cols) FROM PROGRAM 'gzip -dc $f' CSV HEADER
INSERT INTO $t ($cols) SELECT $cols FROM stg ON CONFLICT DO NOTHING;
SQL
    else
      "${PSQL[@]}" -c "\copy $t ($cols) FROM PROGRAM 'gzip -dc $f' CSV HEADER"
    fi
  done
done

"${PSQL[@]}" -f db/seed.sql
echo "building indexes"
"${PSQL[@]}" -f db/indexes.sql
echo done
