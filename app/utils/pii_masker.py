"""
PII (Personally Identifiable Information) Masker

Detects and masks sensitive Thai personal data **before** the message is
forwarded to Claude (or any external AI API).  This prevents PII from
leaving the organisation's perimeter.

Patterns covered:
  - Thai National ID (13 digits, with optional dashes)
  - Thai mobile phone numbers  (06x / 08x / 09x formats)
  - Thai landline / office numbers (0x-xxxx-xxxx)
  - Email addresses
  - Bank account numbers (10–12 digit runs)
  - Passport numbers  (1 letter + 7-8 digits)
  - Credit/debit card numbers (13–19 digits, optionally space/dash separated)

Usage:
  masked_text, report = mask_pii(original_text)
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# ── Pattern definitions ────────────────────────────────────────────────────────

_PATTERNS: list[tuple[str, str, str]] = [
    # (label, placeholder, regex)
    (
        "thai_national_id",
        "[THAI_ID]",
        r"\b\d{1}[-\s]?\d{4}[-\s]?\d{5}[-\s]?\d{2}[-\s]?\d{1}\b",
    ),
    (
        "credit_card",
        "[CARD_NUMBER]",
        # 13-19 digits possibly split by spaces or dashes (Luhn-ish groups)
        r"\b(?:\d[ -]?){13,19}\b",
    ),
    (
        "thai_mobile",
        "[PHONE]",
        r"\b0[689]\d[-\s]?\d{3}[-\s]?\d{4}\b",
    ),
    (
        "thai_phone",
        "[PHONE]",
        r"\b0[2-9]\d{1}[-\s]?\d{3}[-\s]?\d{4}\b",
    ),
    (
        "bank_account",
        "[BANK_ACCOUNT]",
        # Thai bank accounts are typically 10-12 digits
        r"\b\d{3}[-\s]?\d{1}[-\s]?\d{5}[-\s]?\d{1}\b",
    ),
    (
        "email",
        "[EMAIL]",
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    ),
    (
        "passport",
        "[PASSPORT]",
        r"\b[A-Z]{1,2}\d{7,8}\b",
    ),
]

# Pre-compile for performance
_COMPILED: list[tuple[str, str, re.Pattern]] = [
    (label, placeholder, re.compile(pattern, re.IGNORECASE))
    for label, placeholder, pattern in _PATTERNS
]


# ── Result dataclass ───────────────────────────────────────────────────────────


@dataclass
class MaskReport:
    """Summary of what was masked."""

    original_length: int
    masked_length: int
    detections: dict[str, int] = field(default_factory=dict)

    @property
    def has_pii(self) -> bool:
        return bool(self.detections)

    @property
    def total_masked(self) -> int:
        return sum(self.detections.values())


# ── Public API ─────────────────────────────────────────────────────────────────


def mask_pii(text: str) -> tuple[str, MaskReport]:
    """
    Scan *text* for PII and replace each match with a safe placeholder.

    Returns:
        (masked_text, MaskReport)

    Example:
        text = "โทร 089-123-4567 หรือส่งอีเมลมาที่ john@spbt.co.th"
        masked, report = mask_pii(text)
        # masked → "โทร [PHONE] หรือส่งอีเมลมาที่ [EMAIL]"
        # report.detections → {"thai_mobile": 1, "email": 1}
    """
    report = MaskReport(original_length=len(text), masked_length=0, detections={})
    masked = text

    for label, placeholder, pattern in _COMPILED:
        matches = pattern.findall(masked)
        if matches:
            count = len(matches)
            report.detections[label] = report.detections.get(label, 0) + count
            masked = pattern.sub(placeholder, masked)

    report.masked_length = len(masked)
    return masked, report


def has_pii(text: str) -> bool:
    """Quick check — returns True if any PII is detected."""
    for _, _, pattern in _COMPILED:
        if pattern.search(text):
            return True
    return False
