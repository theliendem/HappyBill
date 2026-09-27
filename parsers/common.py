"""Shared helpers: code/payer normalization and gzipped CSV writers (Postgres COPY format)."""
import csv
import gzip
import re
from pathlib import Path

OUT_ROOT = Path(__file__).parent / "out"

# Source ids get a numeric slot so BIGINT ids never collide across files.
ID_BLOCK = 10**11


def norm_key(s):
    """'United_Healthcare' / 'UNITED HEALTHCARE ' -> 'united healthcare'."""
    return re.sub(r"\s+", " ", (s or "").replace("_", " ")).strip().lower()


def norm_code(code, code_type):
    """Normalize a (code, type) pair so hospital files and MRFs join on the same keys."""
    code = (code or "").strip().upper()
    t = (code_type or "").strip().upper()
    if t in ("MS-DRG", "MSDRG", "DRG") and code.isdigit():
        return code.zfill(3), "MS-DRG"
    if t == "APR-DRG":
        return code, "APR-DRG"
    if t in ("CPT", "HCPCS"):
        # CPT is HCPCS Level I (starts with a digit); Level II starts with a letter.
        # Files label these inconsistently, so decide by shape.
        return code, "CPT" if code[:1].isdigit() else "HCPCS"
    if t == "RC":
        return code.zfill(4) if code.isdigit() else code, "RC"
    return code, t


def pg_array(values):
    """Python list -> Postgres array literal for COPY; None/empty -> NULL."""
    values = [str(v) for v in (values or []) if v not in (None, "")]
    if not values:
        return None
    return "{" + ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values) + "}"


class TableWriter:
    """One gzipped CSV per table under out/<source_id>/. Header row = column names."""

    def __init__(self, source_id, tables):
        self.dir = OUT_ROOT / source_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.files, self.writers, self.counts = {}, {}, {}
        for name, cols in tables.items():
            f = gzip.open(self.dir / f"{name}.csv.gz", "wt", newline="", compresslevel=3)
            w = csv.writer(f)
            w.writerow(cols)
            self.files[name], self.writers[name], self.counts[name] = f, w, 0

    def write(self, table, row):
        self.writers[table].writerow(["" if v is None else v for v in row])
        self.counts[table] += 1

    def close(self):
        for f in self.files.values():
            f.close()
        return self.counts
