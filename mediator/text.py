"""Text normalization and trigram similarity for matching bill wording to database descriptions.

Bills and chargemasters abbreviate ("MRI BRAIN W/WO CONTRAST", "HB-ED Level 4 Visit"), and raw
trigram similarity confuses near-identical codes (with vs with-and-without contrast). Expanding
abbreviations first fixes most of that; E/M visit levels are checked separately in codes.py.
"""
import re

_REPLACEMENTS = [
    (r"\b(hb|pb)-", " "),
    (r"\bbefore and after\b", " with and without "),
    (r"\bw\s*/\s*wo\b|\bw/\s*&\s*w/o\b|\bwith\s*(&|and)\s*w/o\b|\bw\s*&\s*wo\b", " with and without "),
    (r"\bw/o\b|\bwo\b|\bw/out\b", " without "),
    (r"\bw/", " with "),
    (r"\bw\b", " with "),
    (r"&", " and "),
    (r"\blvl\b", " level "),
    (r"\blevel (v)\b", " level 5 "),
    (r"\blevel (iv)\b", " level 4 "),
    (r"\blevel (iii)\b", " level 3 "),
    (r"\blevel (ii)\b", " level 2 "),
    (r"\blevel (i)\b", " level 1 "),
    (r"\best\b", " established "),
    (r"\bpt\b", " patient "),
    (r"\b(ed|er)\b", " emergency "),
    (r"\babd\b", " abdomen "),
    (r"\bcbc\b", " complete blood count "),
    (r"\bdiff\b", " differential "),
    (r"\bmetab\b", " metabolic "),
    (r"\bbilat\b", " bilateral "),
    (r"\binj\b", " injection "),
    (r"\bxr\b", " xray "),
    (r"\badv\b", " advantage "),
]


def norm(s):
    s = " " + (s or "").lower().replace("'", "").replace("\u2019", "") + " "
    for pat, rep in _REPLACEMENTS:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_key(s):
    """Same key normalization the parsers use: 'United_Healthcare' -> 'united healthcare'."""
    return re.sub(r"\s+", " ", (s or "").replace("_", " ")).strip().lower()


def trigrams(s):
    """pg_trgm-style trigrams of the normalized string: each word padded with two leading spaces
    and one trailing."""
    out = set()
    for w in norm(s).split():
        w = "  " + w + " "
        out.update(w[i:i + 3] for i in range(len(w) - 2))
    return out


def set_similarity(ta, tb):
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


def similarity(a, b):
    """Trigram similarity of the normalized strings (0..1), equivalent to pg_trgm's similarity()."""
    return set_similarity(trigrams(a), trigrams(b))


# Words every provider name shares; left in, they make "SSM Health St. Mary's Hospital Madison" look like
# "UW Health East Madison Hospital".
_GENERIC_NAME_WORDS = {
    "health", "healthcare", "hospital", "hospitals", "medical", "center", "centers", "centre", "clinic",
    "clinics", "system", "systems", "services", "service", "svc", "care", "group", "the", "of", "and",
    "inc", "llc", "corp", "corporation", "co", "st", "saint", "regional", "memorial", "community",
}


def name_key(s):
    """Provider name reduced to its distinctive words, for name matching."""
    kept = [w for w in norm(s).split() if w not in _GENERIC_NAME_WORDS]
    return " ".join(kept) or norm(s)


def words(s):
    return set(norm(s).split())


def bill_level(s):
    """'ED VISIT LVL 4' -> 4. None if the text names no level."""
    m = re.search(r"\blevel ([1-5])\b", norm(s))
    return int(m.group(1)) if m else None
