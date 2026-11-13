"""Streaming structural FASTQ checks and ordered read-ID fingerprints."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import stat
import zlib
from dataclasses import dataclass
from pathlib import Path

from .inputs import InventoryEntry
from .results import CheckResult


class _HashingReader:
    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        self.bytes_read += len(data)
        return data


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _line(data: bytes) -> str:
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    return data.decode("ascii")


@dataclass
class FastqScan:
    check: CheckResult
    records: int = 0
    read_ids_sha256: str | None = None
    file_sha256: str | None = None


def scan_fastq(root: Path, entry: InventoryEntry, settings: dict) -> FastqScan:
    path = root / entry.relative_path
    check_id = f"FASTQ001:{entry.relative_path}"
    common = dict(check_id=check_id, rule_id="FASTQ001", sample_id=entry.sample_id,
                  file_path=entry.relative_path)
    pattern = re.compile(r"@(?P<read_id>" + settings["read_id_pattern"] + r")/(?P<mate>[12])")
    allowed_bases = set(settings["sequence_alphabet"])
    expected_mate = settings["mate_for_read"][entry.read]
    low, high = settings["quality_ascii_min"], settings["quality_ascii_max"]
    ids_digest = hashlib.sha256()
    records, malformed = 0, 0
    first_problem = None
    complete = False
    file_digest = None
    stream_error = None

    try:
        if path.is_symlink():
            raise OSError("Input changed to a symlink after inventory.")
        with path.open("rb") as raw:
            initial = os.fstat(raw.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise OSError("Input is no longer a regular file.")
            hashed = _HashingReader(raw)
            try:
                with gzip.GzipFile(fileobj=hashed, mode="rb") as compressed:
                    while True:
                        header = compressed.readline()
                        if not header:
                            complete = True
                            break
                        records += 1
                        raw_lines = [header] + [compressed.readline() for _ in range(3)]
                        if any(line == b"" for line in raw_lines):
                            malformed += 1
                            first_problem = first_problem or (records, "Incomplete four-line FASTQ record.")
                            complete = True  # Compressed EOF reached, but the record is invalid.
                            break
                        try:
                            identifier, sequence, separator, qualities = map(_line, raw_lines)
                        except UnicodeDecodeError:
                            malformed += 1
                            first_problem = first_problem or (records, "Record contains non-ASCII bytes.")
                            continue
                        problems = []
                        match = pattern.fullmatch(identifier)
                        if not match:
                            problems.append("Read identifier does not match the configured header format.")
                        elif match["mate"] != expected_mate:
                            problems.append(f"Header mate is {match['mate']}; expected {expected_mate}.")
                        else:
                            # NUL-delimited IDs preserve boundaries and their order.
                            ids_digest.update(match["read_id"].encode("ascii") + b"\0")
                        if not sequence:
                            problems.append("Sequence is empty.")
                        elif set(sequence) - allowed_bases:
                            problems.append("Sequence contains characters outside the configured alphabet.")
                        if separator != settings["separator_line"]:
                            problems.append("Invalid FASTQ separator line.")
                        if len(sequence) != len(qualities):
                            problems.append(
                                f"Sequence length {len(sequence)} does not match quality length {len(qualities)}."
                            )
                        if not qualities:
                            problems.append("Quality string is empty.")
                        elif any(not low <= ord(character) <= high for character in qualities):
                            problems.append("Quality string contains characters outside the configured ASCII range.")
                        if problems:
                            malformed += 1
                            first_problem = first_problem or (records, problems[0])
            except (gzip.BadGzipFile, EOFError, zlib.error) as error:
                stream_error = f"Invalid or truncated gzip stream: {error}"
                first_problem = first_problem or (None, stream_error)
            # Detect ordinary edits/replacements during this scan; no file locking is claimed.
            if path.is_symlink() or _identity(initial) != _identity(os.fstat(raw.fileno())) or _identity(initial) != _identity(path.stat()):
                raise OSError("Input changed while it was being scanned; run validation again.")
            if hashed.bytes_read == initial.st_size:
                file_digest = hashed.digest.hexdigest()
    except OSError as error:
        return FastqScan(CheckResult(
            **common, outcome="ERROR", severity="BLOCKER", message=str(error),
            next_action="Restore readable, stable inputs and rerun validation.",
            details={"records_scanned": records, "scan_complete": False},
        ), records)

    if records < settings["minimum_records_per_file"]:
        first_problem = first_problem or (
            None, f"Only {records} records; at least {settings['minimum_records_per_file']} required."
        )
    details = {"records_scanned": records, "scan_complete": complete,
               "malformed_records": malformed, "stream_error": stream_error}
    if first_problem:
        check = CheckResult(
            **common, outcome="FAIL", severity="BLOCKER", message=first_problem[1],
            record_number=first_problem[0],
            next_action="Investigate and obtain a valid replacement, then rerun validation.",
            details=details,
        )
    else:
        check = CheckResult(**common, outcome="PASS",
                            message=f"All {records} FASTQ records satisfy the configured structural checks.",
                            details=details)
    return FastqScan(check, records, ids_digest.hexdigest(), file_digest)
