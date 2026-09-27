"""Upload parsed sources to the Databricks Volume and (re)build one Delta table per table type.

    .venv/bin/python -m lakehouse.push                      # every folder in parsers/out/
    .venv/bin/python -m lakehouse.push wps_mrf_020          # just these sources
    .venv/bin/python -m lakehouse.push --no-tables          # upload only
    .venv/bin/python -m lakehouse.push --no-upload          # rebuild Delta tables only

Delta tables (e.g. workspace.happybill.prices) union all sources, with a source_dir column.
"""
import sys

from . import CATALOG, ROOT, SCHEMA, VOLUME, client, sql

OUT = ROOT / "parsers" / "out"


def main(args):
    tables = "--no-tables" not in args
    names = [a for a in args if not a.startswith("--")]
    dirs = [OUT / n for n in names] if names else sorted(d for d in OUT.iterdir() if d.is_dir())

    w = client()
    sql(w, f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
    sql(w, f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.sources")

    kinds = set()
    upload = "--no-upload" not in args
    for d in dirs:
        for f in sorted(d.glob("*.csv.gz")):
            kinds.add(f.name.removesuffix(".csv.gz"))
            if not upload:
                continue
            print(f"uploading {d.name}/{f.name} ({f.stat().st_size / 1e6:.1f} MB)")
            with open(f, "rb") as fh:
                w.files.upload(f"{VOLUME}/{d.name}/{f.name}", fh, overwrite=True)

    if tables:
        for t in sorted(kinds):
            print(f"building Delta table {CATALOG}.{SCHEMA}.{t}")
            sql(w, f"""CREATE OR REPLACE TABLE {CATALOG}.{SCHEMA}.{t} AS
                       SELECT regexp_extract(_metadata.file_path, '/sources/([^/]+)/', 1) AS source_dir, *
                       FROM read_files('{VOLUME}/*/{t}.csv.gz', format => 'csv', header => true)""")
    print("done")


if __name__ == "__main__":
    main(sys.argv[1:])
