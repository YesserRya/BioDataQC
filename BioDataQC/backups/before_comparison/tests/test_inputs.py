"""Behavior checks for stage 4. FASTQ content validation is deliberately excluded."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from biodataqc.inputs import DatasetLoadError, load_dataset


class InputLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "dataset"
        self.seq = self.root / "sequencing"
        self.seq.mkdir(parents=True)
        self.config = {
            "specification_version": "1.0.0",
            "project_id": "TEST_001",
            "expected_samples": ["S001", "S002"],
            "metadata": {
                "file": "samples.csv", "encoding": "utf-8", "delimiter": ",",
                "sample_id_column": "sample_id",
            },
            "sequencing": {
                "directory": "sequencing", "layout": "paired_end",
                "reads": ["R1", "R2"], "files_per_read_per_sample": 1,
                "filename_template": "{sample_id}_{read}.fastq.gz",
                "scan_scope": "immediate_children",
            },
        }
        self.write_config()
        self.write_csv(
            ["sample_id", "condition", "tissue", "notes"],
            [["S001", "Control", "Liver", "one"], ["S002", "Treated", "Liver", "two"]],
        )
        for sample in self.config["expected_samples"]:
            for read in ["R1", "R2"]:
                # These are placeholders: stage 4 must not claim they pass FASTQ QC.
                (self.seq / f"{sample}_{read}.fastq.gz").write_bytes(b"inventory fixture")

    def write_config(self):
        (self.root / "requirements.json").write_text(json.dumps(self.config), encoding="utf-8")

    def write_csv(self, headers, rows):
        with (self.root / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(headers)
            writer.writerows(rows)

    def test_complete_inventory_maps_each_sample_and_read(self):
        inputs = load_dataset(self.root)
        self.assertEqual(inputs.summary()["present_expected_files"], 4)
        self.assertEqual(inputs.summary()["missing_expected_files"], 0)
        self.assertEqual(set(inputs.sample_inventory()["R2"]), {"PRESENT"})
        self.assertEqual(inputs.to_dict()["validation_status"], "NOT_VALIDATED")

    def test_missing_mate_is_identified(self):
        (self.seq / "S002_R2.fastq.gz").unlink()
        inputs = load_dataset(self.root)
        missing = [entry for entry in inputs.entries if entry.status == "MISSING"]
        self.assertEqual([(entry.sample_id, entry.read) for entry in missing], [("S002", "R2")])
        self.assertEqual(inputs.summary()["observed_regular_files"], 3)

    def test_same_file_count_does_not_hide_wrong_sample(self):
        (self.seq / "S002_R2.fastq.gz").rename(self.seq / "S999_R2.fastq.gz")
        inputs = load_dataset(self.root)
        self.assertEqual(inputs.summary()["observed_regular_files"], 4)
        self.assertEqual(inputs.summary()["missing_expected_files"], 1)
        self.assertEqual(inputs.summary()["unexpected_entries"], 1)
        extra = next(entry for entry in inputs.entries if not entry.expected)
        self.assertEqual((extra.sample_id, extra.read), ("S999", "R2"))

    def test_csv_values_are_preserved(self):
        self.write_csv(["sample_id", "condition", "tissue", "notes"],
                       [["001", "NA", "", "  text  "]])
        row = load_dataset(self.root).metadata.iloc[0]
        self.assertEqual(row.to_dict(), {"sample_id": "001", "condition": "NA", "tissue": "", "notes": "  text  "})

    def test_duplicate_sample_rows_remain_visible(self):
        self.write_csv(["sample_id", "notes"], [["S001", "one"], ["S001", "two"]])
        table = load_dataset(self.root).sample_inventory().set_index("sample_id")
        self.assertEqual(table.loc["S001", "metadata_rows"], 2)
        self.assertEqual(table.loc["S002", "metadata_rows"], 0)

    def test_missing_id_column_does_not_invent_associations(self):
        self.write_csv(["notes"], [["one"]])
        inputs = load_dataset(self.root)
        self.assertTrue(inputs.sample_inventory()["metadata_rows"].isna().all())
        self.assertEqual(inputs.summary()["expected_files"], 4)

    def test_duplicate_csv_headers_are_rejected(self):
        self.write_csv(["sample_id", "sample_id"], [["S001", "S002"]])
        with self.assertRaisesRegex(DatasetLoadError, "Duplicate CSV"):
            load_dataset(self.root)

    def test_wrong_csv_field_count_is_rejected(self):
        self.write_csv(["sample_id", "notes"], [["S001"]])
        with self.assertRaisesRegex(DatasetLoadError, "fields"):
            load_dataset(self.root)

    def test_duplicate_json_keys_are_rejected(self):
        (self.root / "requirements.json").write_text('{"project_id":"A","project_id":"B"}')
        with self.assertRaisesRegex(DatasetLoadError, "Duplicate JSON"):
            load_dataset(self.root)

    def test_missing_sequencing_directory_is_an_explicit_error(self):
        for entry in self.seq.iterdir():
            entry.unlink()
        self.seq.rmdir()
        with self.assertRaisesRegex(DatasetLoadError, "Sequencing directory"):
            load_dataset(self.root)

    def test_directory_with_expected_filename_is_not_a_present_file(self):
        path = self.seq / "S001_R1.fastq.gz"
        path.unlink()
        path.mkdir()
        inputs = load_dataset(self.root)
        self.assertEqual(inputs.summary()["wrong_type_expected_entries"], 1)
        self.assertEqual(inputs.summary()["present_expected_files"], 3)

    def test_extra_directory_is_reported_without_recursive_scanning(self):
        (self.seq / "extra").mkdir()
        (self.seq / "extra/hidden.fastq.gz").write_bytes(b"extra")
        inputs = load_dataset(self.root)
        self.assertEqual(inputs.summary()["unexpected_entries"], 1)
        self.assertEqual(inputs.summary()["observed_regular_files"], 4)

    def test_symlink_with_expected_filename_is_not_followed(self):
        path = self.seq / "S001_R1.fastq.gz"
        path.unlink()
        path.symlink_to(self.seq / "S001_R2.fastq.gz")
        inputs = load_dataset(self.root)
        entry = next(e for e in inputs.entries if e.relative_path.endswith("S001_R1.fastq.gz"))
        self.assertEqual((entry.status, entry.entry_type), ("WRONG_TYPE", "SYMLINK"))

    def test_configuration_cannot_escape_dataset(self):
        self.config["metadata"]["file"] = "../outside.csv"
        self.write_config()
        with self.assertRaisesRegex(DatasetLoadError, "inside"):
            load_dataset(self.root)

    def test_header_only_metadata_is_loaded_as_zero_rows(self):
        self.write_csv(["sample_id", "notes"], [])
        inputs = load_dataset(self.root)
        self.assertEqual(inputs.summary()["metadata_rows"], 0)
        self.assertEqual(list(inputs.metadata.columns), ["sample_id", "notes"])


if __name__ == "__main__":
    unittest.main()
