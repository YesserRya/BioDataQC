#!/usr/bin/env python3
"""Prepare one small public yeast dataset and a separate missing-mate demo."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from biodataqc.public_data import prepare_public_demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/public_yeast")
    parser.add_argument("--broken-output", type=Path, default=ROOT / "data/public_yeast_missing_mate")
    parser.add_argument("--cache", type=Path, default=ROOT / "outputs/downloads/public_yeast")
    arguments = parser.parse_args()
    try:
        result = prepare_public_demo(arguments.output, arguments.broken_output, arguments.cache)
    except (OSError, ValueError) as error:
        print(f"Preparation failed: {error}", file=sys.stderr)
        return 1
    print("Public-data preparation: COMPLETE")
    print(json.dumps(result, indent=2))
    print("Open Streamlit and select Public yeast, then Run validation to save a run to SQL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
