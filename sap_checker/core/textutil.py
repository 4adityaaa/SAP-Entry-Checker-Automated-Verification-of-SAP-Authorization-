"""Small text helpers: OCR-tolerant normalisation and fuzzy comparison."""
import re

# Characters OCR often confuses. Both sides are mapped before comparing.
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1", "|": "1", "S": "5",
                             "B": "8", "Z": "2", "G": "6", "Q": "0", "D": "0"})


def alnum_key(text: str) -> str:
    """Upper-case and drop everything that is not a letter or digit."""
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def confusable_key(text: str) -> str:
    return alnum_key(text).translate(_CONFUSABLE)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def same_token(a: str, b: str) -> bool:
    """True if two short tokens (T-codes, activities, values) are the same
    after allowing typical OCR confusions (O/0, I/1 ...)."""
    ka, kb = alnum_key(a), alnum_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    if confusable_key(a) == confusable_key(b):
        return True
    # allow one wrong character only for long tokens such as SA38PARAMETER
    return min(len(ka), len(kb)) >= 6 and levenshtein(ka, kb) <= 1


def same_object(a: str, b: str) -> bool:
    """Compare authorization-object names, e.g. S_TCODE vs 'STCODE'."""
    ka, kb = alnum_key(a), alnum_key(b)
    if not ka or not kb:
        return False
    return ka == kb or (min(len(ka), len(kb)) >= 6 and levenshtein(ka, kb) <= 1)


def resolve_name(raw: str, known, max_dist: int = 1):
    """Map an OCR'd name to the closest known name (exact key match first,
    then a unique best match within max_dist for names of length >= 5).
    Returns the known name, or None when nothing is close enough."""
    k = alnum_key(raw)
    if not k:
        return None
    best, best_d, tie = None, 99, False
    for name in known:
        kn = alnum_key(name)
        if kn == k:
            return name
        if min(len(kn), len(k)) >= 5:
            d = levenshtein(k, kn)
            if d < best_d:
                best, best_d, tie = name, d, False
            elif d == best_d:
                tie = True
    if best is not None and best_d <= max_dist and not tie:
        return best
    return None
