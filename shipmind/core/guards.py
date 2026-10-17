"""Input guardrails: anomaly detection for everything that enters the system from a user or an LLM.

Principle: an anomalous value is REJECTED with a reason, never silently coerced or substituted.
(Truncating 2.7 units to 2, or swapping an unknown city for a valid one, ships the wrong thing.)
"""
from __future__ import annotations

import math
import re
import unicodedata
import uuid

from . import config, data

_ORDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
# A standalone number: not glued to letters (TV65, ORD-2001) and keeping its sign ("-3 laptops").
_NUMBER = re.compile(r"(?<![\w-])(-?\d+(?:\.\d+)?)(?=x?\b)(?!\.\d)")
_NUMBER_WORDS = {"one": 1, "single": 1, "two": 2, "pair": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12}
_INJECTION = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|rules|prompts?)",
    r"disregard (?:all |the |your )?(?:instructions|rules|guardrails|system prompt)",
    r"(?:reveal|print|show|repeat) (?:your |the )?(?:system prompt|instructions|api key)",
    r"you are now (?:a|an|in)\b", r"developer mode", r"jailbreak",
    r"(?:skip|bypass|disable|turn off) (?:the )?(?:validat\w*|guardrails?|checks?|approval)",
    r"call (?:the )?(?:dispatch|exit_loop|submit_plan)\w* (?:tool|function)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.I)


def parse_quantity(value) -> tuple[int | None, str | None]:
    """Strict unit count. Returns (qty, None) or (None, reason)."""
    if isinstance(value, bool):
        return None, "quantity must be a number, got a boolean"
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?\d+(?:\.0+)?", value):
            return None, f"quantity '{value}' is not a whole number"
        value = float(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None, f"quantity {value} is not a whole number of units"
        value = int(value)
    if not isinstance(value, int):
        return None, f"quantity has unsupported type {type(value).__name__}"
    if value <= 0:
        return None, f"quantity must be positive, got {value}"
    if value > config.MAX_UNITS_PER_ORDER:
        return None, f"quantity {value} exceeds the per-order limit of {config.MAX_UNITS_PER_ORDER}"
    return value, None


def clean_order_id(value) -> tuple[str | None, str | None]:
    """Use the caller's id if well-formed; generate a collision-free one if none was given."""
    oid = str(value or "").strip()
    if not oid:
        return f"ORD-{uuid.uuid4().hex[:10].upper()}", None
    if not _ORDER_ID.fullmatch(oid):
        return None, "order_id must be 1-40 chars of letters, digits, '.', '_' or '-'"
    return oid, None


def resolve_city(name) -> str | None:
    key = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    return next((c for c in data.CITIES if c.lower() == key), None) or data.CITY_ALIASES.get(key)


def screen_user_text(text: str) -> list[str]:
    """Cheap pre-LLM screen for abusive or malformed requests. Returns the anomalies found."""
    issues = []
    if len(text) > config.MAX_INPUT_CHARS:
        issues.append(f"message is {len(text)} chars (limit {config.MAX_INPUT_CHARS})")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} and ch not in "\n\r\t" for ch in text):
        issues.append("message contains hidden control/format characters")
    if m := _INJECTION_RE.search(text):
        issues.append(f"possible prompt injection: '{m.group(0)}'")
    return issues


def _has_word(text: str, word: str) -> bool:
    """Whole-word match, tolerating plurals ("laptops", "TVs")."""
    return re.search(rf"(?<![\w-]){re.escape(word)}(?:e?s)?(?![\w])", text) is not None


def mentioned_skus(text: str) -> set[str]:
    t = text.lower()
    return {sku for sku, words in data.SKU_KEYWORDS.items() if any(_has_word(t, w) for w in words)}


def grounding_issues(order: dict, user_text: str) -> dict[str, str]:
    """Every order field must be traceable to what the user actually wrote.

    Catches the LLM inventing defaults ("1 unit", "standard"), substituting a "close enough" value for
    an unknown one (Gotham -> Seattle), or dropping a sign ("-3 laptops" -> 3).
    """
    t = user_text.lower()
    issues: dict[str, str] = {}

    numbers = [float(n) for n in _NUMBER.findall(t)]
    words = {_NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", t) if w in _NUMBER_WORDS}
    sku_words = "|".join(re.escape(w) for ws in data.SKU_KEYWORDS.values() for w in ws)
    if re.search(rf"\b(?:a|an)\s+(?:[a-z0-9-]+\s+)?(?:{sku_words})\b", t):  # "a Kindle", "an Echo"
        words.add(1)
    qty = order["quantity"]
    if qty not in numbers and qty not in words:
        bad = [n for n in numbers if n <= 0 or not float(n).is_integer()]
        issues["quantity"] = (f"user wrote {bad[0]:g}, which is not a valid quantity" if bad
                              else f"user never stated a quantity of {qty}")

    skus = mentioned_skus(t)
    if order["sku"] not in skus:
        issues["sku"] = f"user never mentioned {order['sku']}"
    elif len(skus) > 1:
        issues["sku"] = f"request mentions several products ({', '.join(sorted(skus))}); submit one order each"

    city = order["destination_city"]
    names = [city.lower()] + [a for a, c in data.CITY_ALIASES.items() if c == city]
    if not any(_has_word(t, n) for n in names):
        issues["destination_city"] = f"user never mentioned {city}"

    if not any(_has_word(t, w) for w in data.PRIORITY_KEYWORDS[order["priority"]]):
        issues["priority"] = f"user never asked for {order['priority']} delivery"
    return issues
