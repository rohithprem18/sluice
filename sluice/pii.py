"""Reversible PII redaction.

Sensitive values are replaced with typed placeholders (<EMAIL_1>, <CARD_1>, ...)
before a prompt leaves the gateway, and restored in the model's answer on the
way back, so providers never see the originals but the caller still does.
Detectors use checksums where the format has one (Luhn for cards, Verhoeff for
Aadhaar), which keeps false positives on ordinary numbers low.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Verhoeff tables (Aadhaar checksum)
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: re.Pattern
    validate: object = None  # callable(str) -> bool


DETECTORS: list[Detector] = [
    Detector(
        "SECRET",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_\-]{20,}|gsk_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"
        ),
    ),
    Detector("EMAIL", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    Detector(
        "CARD", re.compile(r"\b(?:\d[ \-]?){12,18}\d\b"), lambda s: 13 <= len(_digits(s)) <= 19 and luhn_ok(_digits(s))
    ),
    Detector("AADHAAR", re.compile(r"\b[2-9]\d{3}[ \-]?\d{4}[ \-]?\d{4}\b"), lambda s: verhoeff_ok(_digits(s))),
    Detector("PAN", re.compile(r"\b[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]\b")),
    Detector("SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    Detector("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")),
    Detector(
        "PHONE",
        re.compile(r"(?<![\w+])(?:\+?\d{1,3}[ \-.]?)?(?:\(\d{2,4}\)[ \-.]?)?\d{3,5}[ \-.]?\d{3,4}[ \-.]?\d{0,4}(?!\w)"),
        lambda s: 10 <= len(_digits(s)) <= 13,
    ),
    Detector("IP", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
]


@dataclass
class Redaction:
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class Redactor:
    def __init__(self, detectors: list[Detector] | None = None):
        self.detectors = detectors or DETECTORS

    def redact(self, text: str, state: Redaction | None = None) -> tuple[str, Redaction]:
        state = state or Redaction()
        reverse = {v: k for k, v in state.mapping.items()}
        for det in self.detectors:

            def sub(m: re.Match, det=det) -> str:
                value = m.group(0)
                if value.startswith("<") and value.endswith(">"):
                    return value
                if det.validate and not det.validate(value):
                    return value
                if value in reverse:
                    return reverse[value]
                n = state.counts.get(det.kind, 0) + 1
                state.counts[det.kind] = n
                ph = f"<{det.kind}_{n}>"
                state.mapping[ph] = value
                reverse[value] = ph
                return ph

            text = det.pattern.sub(sub, text)
        return text, state

    def redact_messages(self, messages: list[dict]) -> tuple[list[dict], Redaction]:
        state = Redaction()
        out = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                content, state = self.redact(content, state)
                msg = {**msg, "content": content}
            elif isinstance(content, list):  # multi-part content
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t, state = self.redact(part.get("text", ""), state)
                        part = {**part, "text": t}
                    parts.append(part)
                msg = {**msg, "content": parts}
            out.append(msg)
        return out, state


def restore(text: str, redaction: Redaction) -> str:
    for ph, original in redaction.mapping.items():
        text = text.replace(ph, original)
    return text


class StreamRestorer:
    """Restores placeholders in a token stream, holding back a possible partial '<KIND_n' tail."""

    def __init__(self, redaction: Redaction):
        self.redaction = redaction
        self.buf = ""

    def feed(self, piece: str) -> str:
        if not self.redaction.mapping:
            return piece
        self.buf += piece
        cut = self.buf.rfind("<")
        if cut != -1 and ">" not in self.buf[cut:] and len(self.buf) - cut <= 16:
            ready, self.buf = self.buf[:cut], self.buf[cut:]
        else:
            ready, self.buf = self.buf, ""
        return restore(ready, self.redaction)

    def flush(self) -> str:
        out, self.buf = restore(self.buf, self.redaction), ""
        return out
