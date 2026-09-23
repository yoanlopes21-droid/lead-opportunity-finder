"""Strict, deterministic Val-de-Marne location recognition."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


POSTAL_94 = re.compile(r"(?<!\d)94\d{3}(?!\d)")
COMMUNES_94 = {
    "ablon sur seine", "alfortville", "arcueil", "boissy saint leger", "bonneuil sur marne",
    "bry sur marne", "cachan", "champigny sur marne", "charenton le pont", "chennevieres sur marne",
    "chevilly larue", "choisy le roi", "creteil", "fontenay sous bois", "fresnes", "gentilly",
    "ivry sur seine", "joinville le pont", "la queue en brie", "le kremlin bicetre", "le perreux sur marne",
    "le plessis trevise", "lhay les roses", "limeil brevannes", "maisons alfort", "mandres les roses",
    "marolles en brie", "nogent sur marne", "noiseau", "orly", "ormesson sur marne", "perigny",
    "rungis", "saint mande", "saint maur des fosses", "saint maurice", "santeny", "sucy en brie",
    "thiais", "valenton", "villecresnes", "villejuif", "villeneuve le roi", "villeneuve saint georges",
    "villiers sur marne", "vincennes", "vitry sur seine",
}


def is_val_de_marne(location: Optional[str]) -> bool:
    if not location:
        return False
    normalized = location_key(location)
    return "val de marne" in normalized or bool(POSTAL_94.search(location)) or any(
        re.search(rf"\b{re.escape(commune)}\b", normalized) for commune in COMMUNES_94
    )


def explicit_commune(location: Optional[str]) -> Optional[str]:
    if not location:
        return None
    normalized = location_key(location)
    matches = sorted(
        (commune for commune in COMMUNES_94 if re.search(rf"\b{re.escape(commune)}\b", normalized)),
        key=len,
        reverse=True,
    )
    return matches[0] if matches else None


def location_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    ascii_like = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", ascii_like)).strip()
