from __future__ import annotations

import re

REDACTED = "[REDACTED]"

_ASSIGNMENT = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|authorization|credential)\b"
    r"[\"']?\s*[:=]\s*)(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;#}\]]+)"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_KNOWN_TOKENS = re.compile(
    r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_-]{35}|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,})\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\]}'\"]{8,}")


def redact_text(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    def redact_assignment(match: re.Match) -> str:
        value = match.group("value")
        if value[:1] in {"\"", "'"}:
            quote = value[0]
            return match.group("prefix") + quote + REDACTED + quote
        return match.group("prefix") + REDACTED

    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _ASSIGNMENT.sub(redact_assignment, text)
    text = _KNOWN_TOKENS.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    values = {secret for secret in secrets if isinstance(secret, str) and len(secret) >= 8}
    for secret in sorted(values, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text
