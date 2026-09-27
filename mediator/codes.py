"""Parsing billing codes as printed on bills, and code-type inference."""
import re

from parsers.common import norm_code

CODE_TYPES = ("CPT", "HCPCS", "MS-DRG", "APR-DRG", "EAPG", "RC", "NDC", "LOCAL", "CDM")

_CPT_HCPCS = re.compile(r"\b(\d{4}[0-9A-Z]|[A-V]\d{4})\b")
_MOD = re.compile(r"(?:^|[\s\-,/:;])([0-9A-Z]{2})(?=$|[\s\-,/:;])")


def infer_code(code, code_type=None):
    """(code, type) normalized; type inferred from shape when not given. None if unrecognizable."""
    code = (code or "").strip().upper()
    if not code:
        return None
    if code_type:
        return norm_code(code, code_type)
    if re.fullmatch(r"\d{4}[0-9A-Z]", code):
        return code, "CPT"
    if re.fullmatch(r"[A-V]\d{4}", code):
        return code, "HCPCS"
    if re.fullmatch(r"\d{1,3}", code):
        return code.zfill(3), "MS-DRG"
    if re.fullmatch(r"\d{1,3}-\d", code):
        n, sev = code.split("-")
        return f"{n.zfill(3)}-{sev}", "APR-DRG"
    return None


def parse_printed(text, code_type=None):
    """'99213-25' -> ('99213', 'CPT', ['25']); 'CPT 70553' -> ('70553', 'CPT', []);
    'DRG 470' -> ('470', 'MS-DRG', []). None if no code is found."""
    s = (text or "").upper().strip()
    if not s:
        return None
    m = re.search(r"APR-?DRG\s*[:#]?\s*(\d{1,3})\s*-\s*(\d)", s)
    if m:
        return f"{m.group(1).zfill(3)}-{m.group(2)}", "APR-DRG", []
    m = re.search(r"\b(?:MS-?)?DRG\s*[:#]?\s*(\d{1,3})\b", s)
    if m:
        return m.group(1).zfill(3), "MS-DRG", []
    m = _CPT_HCPCS.search(s)
    if m:
        code, ctype = infer_code(m.group(1), code_type if code_type in ("CPT", "HCPCS") else None)
        mods = [x for x in _MOD.findall(s[m.end():]) if x != code]
        return code, ctype, mods
    inferred = infer_code(s, code_type)
    return (*inferred, []) if inferred else None


def em_level(code):
    """Visit level encoded in E/M codes: 99213 -> 3, 99284 -> 4. None for other codes."""
    if re.fullmatch(r"9920[2-5]|9921[1-5]|9928[1-5]|9922[1-3]|9923[1-3]", code or ""):
        return int(code[-1])
    return None


def em_sibling(code, level):
    """Same E/M family at another level: em_sibling('99214', 3) -> ('99213', 'CPT')."""
    if level is None or em_level(code) is None:
        return None
    sib = code[:-1] + str(level)
    return (sib, "CPT") if em_level(sib) is not None else None
