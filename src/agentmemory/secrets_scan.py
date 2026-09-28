"""Heuristic secret detection for lint checks.

From the Cairn ideas pile (2026-09-23): "Secrets lint: warn if a memory or
handoff looks like it contains a token or password."

Deliberately a warn-only heuristic scanner, not a hard gate -- false
positives here just mean an extra line in `brainctl lint` output, not a
blocked write. Two families of pattern:

1. Well-known credential shapes (provider-specific prefixes/lengths) --
   low false-positive rate, these basically only match real credentials.
2. Generic "key/token/password/secret: <value>" phrasing -- higher recall,
   some false-positive risk (e.g. a memory that quotes someone else's
   sentence containing the word "password"), which is exactly why this is
   a lint warning and not a write-time refusal.
"""
from __future__ import annotations

import re
from typing import List, NamedTuple


class SecretMatch(NamedTuple):
    pattern_name: str
    redacted: str


# Well-known provider credential shapes.
_KNOWN_PATTERNS = [
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("discord_bot_token", re.compile(r"\b[MN][A-Za-z\d]{23,}\.[\w-]{6,7}\.[\w-]{27,}\b")),
]

# Generic "label: value" phrasing -- catches the common "password: hunter2"
# / "api_key = abc123..." shape regardless of provider. Requires the value
# to be at least 6 chars of non-whitespace so it doesn't fire on "password:
# (not set)" or similar placeholder text.
#
# The label boundary is (?:^|[\s_]) / (?=[\s_]|$) rather than \b on both
# sides -- caught in a backward-adversarial self-review, 2026-09-28: a
# plain \b(...)\b does NOT match "TOKEN" inside "DISCORD_BOT_TOKEN", because
# underscore counts as a word character in regex, so there's no boundary
# between "_" and "T". That's a real, common shape (SCREAMING_SNAKE_CASE
# env var names) -- the original version of this pattern would have missed
# the literal Discord bot token sitting in this machine's own primary
# config file. Treating "_" as an additional valid separator, alongside
# whitespace and string start/end, catches that shape without opening up
# false positives on a label embedded mid-word without any separator
# (e.g. "atoken:" still doesn't match).
_GENERIC_LABEL_PATTERN = re.compile(
    r"(?:^|[\s_])(password|passwd|api[_-]?key|secret|token|auth[_-]?token|bearer)(?=[\s_:=]|$)"
    r"\s*[:=]\s*['\"]?(\S{6,})['\"]?",
    re.IGNORECASE,
)

# Placeholder values that should never trigger a warning even though they
# match the generic label pattern -- documentation/examples, not real
# secrets. Checked case-insensitively against the captured value.
_PLACEHOLDER_VALUES = {
    "changeme", "your_key_here", "xxxxxxxx", "redacted", "not_set", "none",
    "null", "<redacted>", "placeholder", "example", "todo", "tbd",
}


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "…" + value[-4:]


def scan_for_secrets(text: str) -> List[SecretMatch]:
    """Scan a block of text for anything that looks like a credential.
    Returns a list of (pattern_name, redacted_preview) matches -- never
    the raw matched secret itself, so a lint report is safe to print or
    log."""
    if not text:
        return []

    matches: List[SecretMatch] = []

    for name, pattern in _KNOWN_PATTERNS:
        for m in pattern.finditer(text):
            matches.append(SecretMatch(name, _redact(m.group(0))))

    for m in _GENERIC_LABEL_PATTERN.finditer(text):
        value = m.group(2)
        if value.strip("'\"").lower() in _PLACEHOLDER_VALUES:
            continue
        matches.append(SecretMatch(f"generic_{m.group(1).lower()}", _redact(value)))

    return matches
