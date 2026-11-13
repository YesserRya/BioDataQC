"""Explicit FASTQ header profiles; original file bytes are never rewritten."""

from __future__ import annotations

import re


PUBLIC_HEADER_PROFILES = ("sra_spot_mate", "slash_mate", "illumina_casava18")
_PATTERNS = {
    "synthetic_v1": re.compile(r"@(?P<read_id>READ_[0-9]{6})/(?P<mate>[12])"),
    # The second token repeats the SRA spot number and explicitly identifies the mate.
    # Example: @SRR6357070.123 123/1 kraken:taxid|4932
    "sra_spot_mate": re.compile(
        r"@(?P<read_id>[SED]RR[0-9]+\.(?P<spot>[1-9][0-9]*))"
        r"[ \t]+(?P=spot)/(?P<mate>[12])(?:[ \t]+[ -~]+)?"),
    "slash_mate": re.compile(r"@(?P<read_id>[!-~]+?)/(?P<mate>[12])(?:[ \t]+[ -~]+)?"),
    "illumina_casava18": re.compile(
        r"@(?P<read_id>[!-~]+)[ \t]+(?P<mate>[12]):[YN]:[0-9]+:[!-~]+"),
}


def parse_read_header(identifier: str, settings: dict) -> tuple[str, str]:
    """Return the exact pair key and explicit mate; never infer mates from filenames."""
    profile = settings.get("header_profile", "synthetic_v1")
    if profile not in _PATTERNS:
        raise ValueError(f"Unsupported FASTQ header profile: {profile!r}.")
    match = _PATTERNS[profile].fullmatch(identifier)
    if match is None:
        raise ValueError(f"Read identifier does not match the configured header format ({profile}).")
    return match["read_id"], match["mate"]


def separator_matches(separator: str, identifier: str, mode: str) -> bool:
    if mode == "+":
        return separator == "+"
    if mode == "plus_or_matching_header":
        return separator in {"+", "+" + identifier[1:]}
    return False
