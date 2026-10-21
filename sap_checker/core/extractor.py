"""Turns raw OCR boxes of a SUIM 'Users by Complex Selection Criteria' screen
into structured blocks:

    [{"object": "S_TABU_DIS", "fields": {"DICBERCLS": ["SS"], "ACTVT": ["02"]}}, ...]

Only authorization objects in the workbook are emitted; unrecognized object
sections are ignored rather than mistaken for screenshot continuations.
"""
import re
from difflib import SequenceMatcher
from statistics import median
from typing import Dict, List

import numpy as np

from .textutil import alnum_key, resolve_name, same_token

_HEADER_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]{1,15})\s*[-\u2013\u2014~]\s*\S")
_OBJ_LABEL_RE = re.compile(r"^\s*(?:and\s+)?auth\w*\s+obj\w*(.*)$", re.I)
_FOOTER_RE = re.compile(
    r"(?:type\s+here\s+to\s+search|document\s*\d*\s*[-\u2013\u2014]\s*word)", re.I
)
_LABEL_KEYS = {"OR", "AND", "0R", "DR", "QR"}


def _is_label(token: str) -> bool:
    k = alnum_key(token)
    if k in _LABEL_KEYS:
        return True
    return 4 <= len(k) <= 6 and k.isalpha() and SequenceMatcher(None, k, "VALUE").ratio() >= 0.72


def _clean_value(token: str) -> str:
    t = token.strip().strip("[]()|,;:'\"` ")
    if not t:
        return ""
    if not re.search(r"[A-Za-z0-9*]", t):
        return ""
    return t[:30].upper()


def _cluster_lines(items: List[dict]) -> List[List[dict]]:
    if not items:
        return []
    h_med = median(i["h"] for i in items)
    items = sorted(items, key=lambda i: i["cy"])
    lines: List[List[dict]] = []
    means: List[float] = []
    for it in items:
        if lines and abs(it["cy"] - means[-1]) <= 0.6 * h_med:
            lines[-1].append(it)
            means[-1] = sum(x["cy"] for x in lines[-1]) / len(lines[-1])
        else:
            lines.append([it])
            means.append(it["cy"])
    return [sorted(l, key=lambda i: i["x0"]) for l in lines]


def extract_blocks(ocr_items, known_objects, known_fields, known_values=None,
                   known_field_values=None) -> Dict:
    """Parse OCR boxes, optionally retaining only values required by the workbook."""
    items = []
    for idx, (box, text, score) in enumerate(ocr_items):
        box = np.asarray(box, dtype=float)
        h = (np.linalg.norm(box[3] - box[0]) + np.linalg.norm(box[2] - box[1])) / 2
        items.append({"idx": idx, "text": text.strip(), "score": score,
                      "cx": box[:, 0].mean(), "cy": box[:, 1].mean(),
                      "x0": box[:, 0].min(), "h": max(h, 1.0)})
    items = [i for i in items if i["text"] and i["score"] >= 0.30]
    lines = _cluster_lines(items)

    all_fields = set(known_fields)
    blocks: List[Dict] = []
    cur_block = None
    cur_field = None
    ignoring_unknown_object = False
    used_idx = set()
    text_lines = []

    for line in lines:
        line_text = " ".join(i["text"] for i in line)
        text_lines.append(line_text)

        if _FOOTER_RE.search(line_text):
            cur_field = None
            break

        # ---- 1. "Authorization Object  S_TCODE" -------------------------------
        m = _OBJ_LABEL_RE.match(line_text)
        if m:
            rest = m.group(1).strip()
            cur_field = None
            cands = [t for t in rest.split() if t]
            if not cands or re.fullmatch(r"\d{1,2}", rest):
                cur_block = None          # "AND authorization object 2" or blank field
                ignoring_unknown_object = True
                continue
            options = sorted(cands, key=len, reverse=True) + ["".join(cands), "_".join(cands)]
            name = None
            for opt in options:
                name = resolve_name(opt, known_objects, max_dist=1)
                if name:
                    break
            if name:
                cur_block = {"object": name, "fields": {}}
                blocks.append(cur_block)
                ignoring_unknown_object = False
                for i in line:
                    if not alnum_key(i["text"]).startswith("AUTH") and alnum_key(i["text"]) != "OBJECT":
                        used_idx.add(i["idx"])
            else:
                cur_block = None
                ignoring_unknown_object = True
            continue

        # ---- 2. field header "ACTVT - Activity" --------------------------------
        hm = _HEADER_RE.match(line_text)
        fname = resolve_name(hm.group(1).upper(), all_fields, 1) if hm else None
        if hm and fname is None:
            cur_field = None
            continue
        if not hm:
            toks = line_text.split()
            if len(toks) >= 2 and resolve_name(toks[0], all_fields, 1) \
                    and len(alnum_key(toks[0])) >= 3 \
                    and re.search(r"[A-Za-z]{4,}", " ".join(toks[1:])):
                fname = resolve_name(toks[0], all_fields, 1)
        if fname:
            cur_field = fname
            if cur_block is None and not ignoring_unknown_object:
                # The screenshot begins in the middle of a known block.
                cur_block = {"object": None, "fields": {}}
                blocks.append(cur_block)
            if cur_block is not None:
                cur_block["fields"].setdefault(cur_field, [])
            used_idx.update(item["idx"] for item in line)
            continue

        # ---- 3. value rows  "Value  RZ10  OR  RZ11" / "AND ..." ------------------
        if cur_block is not None and cur_field:
            for it in line:
                for tok in it["text"].split():
                    if _is_label(tok):
                        continue
                    val = _clean_value(tok)
                    if val:
                        if known_field_values is not None:
                            candidates = known_field_values.get(cur_field, set())
                        else:
                            candidates = known_values
                        if candidates is not None and not _is_known_value(val, candidates):
                            continue
                        vals = cur_block["fields"].setdefault(cur_field, [])
                        if val not in vals:
                            vals.append(val)
                        used_idx.add(it["idx"])

    used_scores = [item["score"] for item in items if item["idx"] in used_idx]
    return {
        "blocks": blocks,
        "lines": text_lines,
        "used": sorted(used_idx),
        "items": items,
        "confidence": (sum(used_scores) / len(used_scores)) if used_scores else None,
        "confidence_count": len(used_scores),
    }


def _is_known_value(value: str, known_values) -> bool:
    """Reject unrelated screen text and ambiguous OCR corrections."""
    star_aliases = {"*", "**", "\uff0a", "\u00d7", "+"}
    if value in star_aliases:
        return any(candidate in star_aliases for candidate in known_values)

    exact = [candidate for candidate in known_values
             if alnum_key(candidate) == alnum_key(value)]
    if exact:
        return True
    return sum(1 for candidate in known_values if same_token(value, candidate)) == 1
