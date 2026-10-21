"""Matches every Excel requirement row against the data extracted from the screenshots."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .excel_parser import FieldReq, ObjectReq, RowReq, tree_eval
from .textutil import alnum_key, same_token

STAR_ALIASES = {"*", "**", "\uff0a", "\u00d7", "+"}


def _value_present(required: str, found: List[str]) -> bool:
    if required == "*":
        return any(f in STAR_ALIASES for f in found)
    return any(same_token(required, f) for f in found)


def _find_field(block_fields: Dict[str, List[str]], name: str) -> Optional[List[str]]:
    k = alnum_key(name)
    for fname, vals in block_fields.items():
        if alnum_key(fname) == k:
            return vals
    return None


@dataclass
class FieldResult:
    name: str
    expected: str
    found: List[str]
    missing: List[str]
    ok: bool


@dataclass
class ObjectResult:
    obj: str
    ok: bool
    screenshot: str = ""
    fields: List[FieldResult] = field(default_factory=list)
    note: str = ""
    screenshots: List[str] = field(default_factory=list)


def _check_field(freq: FieldReq, block_fields, strict: bool) -> FieldResult:
    found_vals = _find_field(block_fields, freq.name)
    if found_vals is None:
        return FieldResult(freq.name, freq.raw, [], list(freq.values), False)
    present = [v for v in freq.values if _value_present(v, found_vals)]
    missing = [v for v in freq.values if v not in present]
    if strict:
        ok = not missing
    else:
        ok = tree_eval(freq.tree, lambda v: _value_present(v, found_vals))
    return FieldResult(freq.name, freq.raw, present, missing, ok)


def _score(results: List[FieldResult]) -> float:
    tot = sum(len(r.found) + len(r.missing) for r in results) or 1
    return sum(len(r.found) for r in results) / tot


def check_object(oreq: ObjectReq, screens: List[dict], strict: bool) -> ObjectResult:
    best: Optional[ObjectResult] = None
    best_score = -1.0
    seen_object = False
    for scr in screens:
        for blk in scr["blocks"]:
            if blk["object"] is None or alnum_key(blk["object"]) != alnum_key(oreq.obj):
                continue
            seen_object = True
            if oreq.fields:
                fres = [_check_field(f, blk["fields"], strict) for f in oreq.fields]
            else:
                fres = []
            ok = all(f.ok for f in fres)
            score = (1.0 if ok else 0.0) * 10 + (_score(fres) if fres else 1.0)
            if score > best_score:
                best_score = score
                evidence_screens = blk.get("evidence_screens", [scr["filename"]])
                best = ObjectResult(
                    oreq.obj, ok, scr["filename"], fres,
                    screenshots=list(evidence_screens),
                )
    if best is None:
        return ObjectResult(oreq.obj, False, "", [], "Authorization object not found in any screenshot")
    if not best.ok:
        best.note = "; ".join(
            f"{f.name}: " + (f"missing {', '.join(f.missing)}" if f.missing else "condition not satisfied")
            for f in best.fields if not f.ok)
    return best


def match_all(reqs: List[RowReq], screens: List[dict], strict: bool = True) -> List[dict]:
    out = []
    for r in reqs:
        objs = [check_object(o, screens, strict) for o in r.objects]
        status = "PASS" if all(o.ok for o in objs) else "FAIL"
        evidence = sorted({
            screenshot
            for obj in objs
            for screenshot in (obj.screenshots or ([obj.screenshot] if obj.screenshot else []))
        })
        reason = "" if status == "PASS" else " | ".join(
            f"{o.obj}: {o.note}" for o in objs if not o.ok)
        out.append({
            "excel_row": r.excel_row, "rafit": r.rafit, "function": r.function,
            "status": status, "evidence": evidence, "reason": reason,
            "objects": [{
                "obj": o.obj, "ok": o.ok, "screenshot": o.screenshot, "note": o.note,
                "screenshots": o.screenshots,
                "fields": [{"name": f.name, "expected": f.expected, "found": f.found,
                            "missing": f.missing, "ok": f.ok} for f in o.fields]}
                for o in objs],
        })
    return out
