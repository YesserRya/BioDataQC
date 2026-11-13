#!/usr/bin/env python3
"""Generate BioDataQC v1 synthetic fixtures using only Python's standard library.

Save this file as scripts/generate_demo_data.py inside the project.
Run from the project root:
    python scripts/generate_demo_data.py

Defaults: config/requirements.json -> data/clean and data/broken.
The fixtures contain synthetic reads only; they do not simulate gene expression.
Identical settings and the same Python/zlib environment produce identical bytes.
Existing identical fixtures are left alone; edited files are never overwritten.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import random
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_IDS = [f"S{number:03d}" for number in range(1, 7)]
MISSING_FILE = "S003_R2.fastq.gz"
MALFORMED_FILE = "S004_R1.fastq.gz"
COMPLEMENT = str.maketrans("ACGT", "TGCA")


def read_requirements(path: Path) -> tuple[dict, bytes]:
    """Require the agreed fixture profile rather than guessing other formats."""
    raw = path.read_bytes()
    config = json.loads(raw.decode("utf-8"))
    if not isinstance(config, dict):
        raise ValueError("requirements.json must contain a JSON object.")
    if config.get("synthetic_data_only") is not True:
        raise ValueError("The configuration must declare synthetic_data_only=true.")
    if config.get("expected_samples") != SAMPLE_IDS:
        raise ValueError("This v1 fixture generator expects S001 through S006 in order.")

    expected_settings = {
        "metadata": {
            "file": "samples.csv",
            "encoding": "utf-8",
            "delimiter": ",",
            "one_row_per_sample": True,
            "sample_id_column": "sample_id",
            "required_columns": ["sample_id", "condition", "tissue"],
            "optional_columns": ["notes"],
            "allowed_values": {"condition": ["Control", "Treated"]},
        },
        "sequencing": {
            "directory": "sequencing",
            "layout": "paired_end",
            "reads": ["R1", "R2"],
            "files_per_read_per_sample": 1,
            "filename_template": "{sample_id}_{read}.fastq.gz",
            "compression": "gzip",
        },
        "fastq": {
            "format": "four_line",
            "encoding": "ascii",
            "sequence_alphabet": "ACGTN",
            "separator_line": "+",
            "quality_encoding": "phred33",
            "header_template": "@{read_id}/{mate}",
            "read_id_pattern": "READ_[0-9]{6}",
            "mate_for_read": {"R1": "1", "R2": "2"},
            "minimum_records_per_file": 1,
        },
    }
    for section, settings in expected_settings.items():
        actual = config.get(section)
        if not isinstance(actual, dict):
            raise ValueError(f"Missing configuration section: {section}")
        for key, value in settings.items():
            if actual.get(key) != value:
                raise ValueError(
                    f"Unsupported fixture setting: {section}.{key}. "
                    "Use the agreed v1 configuration or adapt the generator."
                )
    fastq = config["fastq"]
    if not fastq["quality_ascii_min"] <= ord("I") <= fastq["quality_ascii_max"]:
        raise ValueError("The configured quality range must accept the character I.")
    return config, raw


@contextmanager
def write_fastq(path: Path):
    """Fix gzip time/name headers so repeated runs are byte-reproducible."""
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6
        ) as compressed:
            with io.TextIOWrapper(compressed, encoding="ascii", newline="\n") as text:
                yield text


def create_clean_dataset(
    destination: Path, config: dict, config_bytes: bytes,
    rng: random.Random, records: int, read_length: int,
) -> None:
    sequencing = destination / config["sequencing"]["directory"]
    sequencing.mkdir(parents=True)
    (destination / "requirements.json").write_bytes(config_bytes)

    metadata = config["metadata"]
    columns = metadata["required_columns"] + metadata["optional_columns"]
    with (destination / metadata["file"]).open(
        "w", encoding=metadata["encoding"], newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, delimiter=metadata["delimiter"],
            lineterminator="\n",
        )
        writer.writeheader()
        for index, sample_id in enumerate(config["expected_samples"]):
            writer.writerow({
                "sample_id": sample_id,
                "condition": "Control" if index < 3 else "Treated",
                "tissue": "Liver",
                "notes": f"Synthetic fixture for {sample_id}; no real sample",
            })

    template = config["sequencing"]["filename_template"]
    quality = "I" * read_length
    for sample_id in config["expected_samples"]:
        r1_path = sequencing / template.format(sample_id=sample_id, read="R1")
        r2_path = sequencing / template.format(sample_id=sample_id, read="R2")
        with write_fastq(r1_path) as r1, write_fastq(r2_path) as r2:
            for number in range(1, records + 1):
                # Two ends of a random fragment; the mate reads have matching IDs.
                fragment = "".join(rng.choices("ACGT", k=2 * read_length + 50))
                sequence1 = fragment[:read_length]
                sequence2 = fragment[-read_length:].translate(COMPLEMENT)[::-1]
                read_id = f"READ_{number:06d}"
                r1.write(f"@{read_id}/1\n{sequence1}\n+\n{quality}\n")
                r2.write(f"@{read_id}/2\n{sequence2}\n+\n{quality}\n")


def inject_demo_errors(broken: Path) -> None:
    sequencing = broken / "sequencing"
    (sequencing / MISSING_FILE).unlink()

    target = sequencing / MALFORMED_FILE
    temporary = target.with_name(target.name + ".tmp")
    with gzip.open(target, "rt", encoding="ascii") as source:
        first_record = [source.readline() for _ in range(4)]
        first_record[1] = first_record[1].rstrip("\r\n")[:12] + "\n"
        first_record[3] = first_record[3].rstrip("\r\n")[:8] + "\n"
        with write_fastq(temporary) as output:
            output.writelines(first_record)
            shutil.copyfileobj(source, output)
    temporary.replace(target)


def tree_signature(directory: Path) -> dict:
    """Compare directory contents before deciding whether an existing copy is safe."""
    signature = {}
    for entry in sorted(directory.rglob("*")):
        relative = entry.relative_to(directory).as_posix()
        if entry.is_symlink():
            raise ValueError(f"Refusing to replace a dataset containing a symlink: {entry}")
        if entry.is_dir():
            signature[relative] = ("directory",)
        elif entry.is_file():
            with entry.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            signature[relative] = ("file", digest)
        else:
            raise ValueError(f"Unsupported filesystem entry: {entry}")
    return signature


def publish_datasets(staging: Path, output_dir: Path) -> dict[str, str]:
    """Preflight both targets; never erase edited or unrelated data."""
    actions = {}
    for name in ("clean", "broken"):
        target = output_dir / name
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise ValueError(f"Expected a normal dataset directory at: {target}")
        if not target.exists() or not any(target.iterdir()):
            actions[name] = "created"
        elif tree_signature(target) == tree_signature(staging / name):
            actions[name] = "unchanged"
        else:
            raise ValueError(
                f"{target} already contains different files; nothing was replaced. "
                "Use --output-dir with a new location to generate another copy."
            )

    for name, action in actions.items():
        if action == "created":
            target = output_dir / name
            if target.exists():
                target.rmdir()  # Only the empty directory checked above.
            (staging / name).rename(target)
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=PROJECT_ROOT / "config" / "requirements.json",
        help="Client configuration (default: project/config/requirements.json)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "data",
        help="Parent of clean/ and broken/ (default: project/data)",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--records", type=int, default=1000,
                        help="Read pairs per sample (default: 1000)")
    parser.add_argument("--read-length", type=int, default=100,
                        help="Bases per clean read (default: 100)")
    args = parser.parse_args()
    if not 1 <= args.records <= 999999:
        parser.error("--records must be between 1 and 999999 for six-digit read IDs.")
    if not 12 <= args.read_length <= 1000:
        parser.error("--read-length must be between 12 and 1000.")

    try:
        config, config_bytes = read_requirements(args.config)
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".biodataqc-generate-", dir=output_dir) as temporary:
            staging = Path(temporary)
            create_clean_dataset(
                staging / "clean", config, config_bytes, random.Random(args.seed),
                args.records, args.read_length,
            )
            shutil.copytree(staging / "clean", staging / "broken")
            inject_demo_errors(staging / "broken")
            actions = publish_datasets(staging, output_dir)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Project: {config['project_id']} | seed: {args.seed}")
    print(f"Samples: {len(config['expected_samples'])} | read pairs per sample: {args.records}")
    print(f"Clean read length: {args.read_length} bases")
    for name in ("clean", "broken"):
        dataset = output_dir / name
        count = len(list((dataset / "sequencing").glob("*.fastq.gz")))
        print(f"{name}: {count} FASTQ files | {actions[name]} | {dataset}")
    print(f"Deliberate missing file: broken/sequencing/{MISSING_FILE}")
    print(f"Deliberate malformed record: {MALFORMED_FILE}, record 1, 12 bases / 8 quality characters")
    print("Dataset generation finished. Application validation will be implemented in later stages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
