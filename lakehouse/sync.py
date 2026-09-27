"""Pull sources from the Databricks Volume into Postgres: any source_id not already in `sources` is
downloaded and loaded with db/load.sh. Every run is recorded in the sync_log table.

    .venv/bin/python -m lakehouse.sync            # load new sources
    .venv/bin/python -m lakehouse.sync --dry-run  # just list what's new
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg

from . import ROOT, VOLUME, client

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres@localhost:5432/claritybill")

SYNC_LOG = """CREATE TABLE IF NOT EXISTS sync_log (
    id         SERIAL PRIMARY KEY,
    synced_at  TIMESTAMPTZ DEFAULT now(),
    remote     TEXT NOT NULL,
    checked    INT NOT NULL,
    loaded     TEXT[] NOT NULL
)"""


def main(args):
    w = client()
    remote = sorted(e.name for e in w.files.list_directory_contents(VOLUME) if e.is_directory)
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        local = {r[0] for r in conn.execute("SELECT source_id FROM sources")}
        new = [s for s in remote if s not in local]
        print(f"{len(remote)} sources on Databricks, {len(new)} new: {', '.join(new) or '-'}")
        if "--dry-run" in args:
            return

        with tempfile.TemporaryDirectory() as tmp:
            for s in new:
                d = Path(tmp) / s
                d.mkdir()
                for e in w.files.list_directory_contents(f"{VOLUME}/{s}"):
                    print(f"downloading {s}/{e.name}")
                    with open(d / e.name, "wb") as fh:
                        fh.write(w.files.download(e.path).contents.read())
            if new:
                subprocess.run([str(ROOT / "db" / "load.sh"), *(str(Path(tmp) / s) for s in new)], check=True)

        conn.execute(SYNC_LOG)
        conn.execute("INSERT INTO sync_log (remote, checked, loaded) VALUES (%s, %s, %s)",
                     (VOLUME, len(remote), new))
    print("done")


if __name__ == "__main__":
    main(sys.argv[1:])
