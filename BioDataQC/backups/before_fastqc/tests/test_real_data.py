"""Public-format behavior and import boundaries; unit fixtures contain synthetic bases.

No network is required by this test suite. The downloaded experimental fixture is
also exercised separately in the documented integration workflow.
"""

import copy
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from biodataqc.history import get_run, list_packages, list_runs, save_run
from biodataqc.packaging import PackagingError, _input_plan, build_package, verify_package
from biodataqc.public_data import CATALOG, PreparationError, prepare_public_demo
from biodataqc.read_headers import parse_read_header, separator_matches
from biodataqc.reporting import render_report
from biodataqc.validation import _configuration, validate_dataset


ROOT = Path(__file__).resolve().parent.parent


class HeaderProfileTests(unittest.TestCase):
    def test_explicit_profiles_extract_exact_pair_keys_and_mates(self):
        examples = [
            ("synthetic_v1", "@READ_000001/1", "READ_000001", "1"),
            ("sra_spot_mate", "@SRR6357070.123 123/2 kraken:taxid|4932", "SRR6357070.123", "2"),
            ("slash_mate", "@instrument:run:123/1 annotation", "instrument:run:123", "1"),
            ("illumina_casava18", "@M00900:62:A2CYG:1:1101:18016:2491 2:N:0:13", "M00900:62:A2CYG:1:1101:18016:2491", "2"),
        ]
        for profile, header, key, mate in examples:
            with self.subTest(profile=profile):
                self.assertEqual(parse_read_header(header, {"header_profile": profile}), (key, mate))

    def test_sra_repeated_spot_must_agree(self):
        with self.assertRaises(ValueError):
            parse_read_header("@SRR6357070.123 456/1", {"header_profile": "sra_spot_mate"})

    def test_sra_spot_suffix_is_never_stripped_as_a_mate(self):
        first = parse_read_header("@SRR6357070.1 1/1", {"header_profile": "sra_spot_mate"})
        second = parse_read_header("@SRR6357070.2 2/2", {"header_profile": "sra_spot_mate"})
        self.assertNotEqual(first[0], second[0])

    def test_unmarked_sra_mates_are_rejected_instead_of_inferred(self):
        for text in ("@SRR6357070.123", "@SRR6357070.123 length=101", "@SRR6357070.123 123/3"):
            with self.subTest(header=text), self.assertRaises(ValueError):
                parse_read_header(text, {"header_profile": "sra_spot_mate"})

    def test_legacy_profile_does_not_accept_real_identifiers(self):
        with self.assertRaises(ValueError):
            parse_read_header("@SRR6357070.123 123/1", {})

    def test_unknown_profile_and_control_characters_are_rejected(self):
        cases = [("unknown", "@x/1"), ("slash_mate", "@x\0/1"),
                 ("illumina_casava18", "@x 1:N:0:"), ("sra_spot_mate", "@SRR1.1 1/1\n")]
        for profile, text in cases:
            with self.subTest(profile=profile, header=text), self.assertRaises(ValueError):
                parse_read_header(text, {"header_profile": profile})

    def test_separator_may_repeat_only_the_exact_header(self):
        header = "@SRR6357070.123 123/1 kraken:taxid|4932"
        self.assertTrue(separator_matches("+", header, "plus_or_matching_header"))
        self.assertTrue(separator_matches("+" + header[1:], header, "plus_or_matching_header"))
        self.assertFalse(separator_matches("+SRR6357070.999", header, "plus_or_matching_header"))
        self.assertFalse(separator_matches("+" + header[1:], header, "+"))


class PublicFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "data/public_yeast"
        self.root.mkdir(parents=True)
        (self.root / "sequencing").mkdir()
        self.db = self.base / "outputs/biodataqc.sqlite"
        self.destination = self.base / "outputs/packages/public"
        self.config = json.loads(CATALOG.read_text())
        self.config["fastq"]["minimum_records_per_file"] = 3
        self.config["provenance"]["expected_records_per_mate"] = 3
        self.write_reads()
        with (self.root / "samples.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.config["metadata"]["required_columns"] + ["notes"])
            writer.writerow(["SRR6357070", "Saccharomyces cerevisiae", "SRR6357070", "GSM2879618", "Wild-type", "Synthetic unit-test reads"])
        self.update_references()

    def write_reads(self, *, order=(1, 2, 3), second_order=None, repeat_separator=False):
        profile = self.config["fastq"]["header_profile"]
        for mate in (1, 2):
            rows = []
            for number in (second_order if mate == 2 and second_order is not None else order):
                if profile == "sra_spot_mate":
                    header = f"@SRR6357070.{number} {number}/{mate} kraken:taxid|4932"
                elif profile == "slash_mate":
                    header = f"@instrument:run:{number}/{mate} extra"
                else:
                    header = f"@instrument:1:flowcell:1:1:1:{number} {mate}:N:0:ATCG"
                separator = "+" + header[1:] if repeat_separator else "+"
                rows.append(f"{header}\nACGTN\n{separator}\nIIIII\n")
            self.path(mate).write_bytes(gzip.compress("".join(rows).encode(), mtime=0))

    def path(self, mate):
        return self.root / f"sequencing/SRR6357070_R{mate}.fastq.gz"

    def update_references(self):
        for item in self.config["provenance"]["files"]:
            data = (self.root / item["local_path"]).read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            item.update(size_bytes=len(data), sha256=sha,
                        git_blob_sha1=hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest())
            self.config["integrity"]["reference_sha256"][item["local_path"]] = sha
        self.write_config()

    def write_config(self):
        (self.root / "requirements.json").write_text(json.dumps(self.config))

    def checks(self):
        report = validate_dataset(self.root)
        return report, {c.check_id: c for c in report.checks}


class PublicValidationTests(PublicFixture):
    def test_real_profile_ready_includes_required_source_comparison(self):
        report, checks = self.checks()
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.counts()["records_scanned"], 6)
        self.assertEqual(checks["SRC001"].outcome, "PASS")
        self.assertTrue(checks["SRC001"].required)
        self.assertEqual(checks["REF001"].outcome, "NOT_RUN")
        self.assertFalse(checks["REF001"].required)
        self.assertEqual(report.to_dict()["rules_version"], "1.1.0")

    def test_all_supported_public_header_profiles_validate_pairs(self):
        for profile in ("sra_spot_mate", "slash_mate", "illumina_casava18"):
            with self.subTest(profile=profile):
                self.config["fastq"]["header_profile"] = profile
                self.write_reads()
                self.update_references()
                self.assertEqual(validate_dataset(self.root).status, "READY")

    def test_repeated_separator_is_supported_without_rewriting(self):
        self.write_reads(repeat_separator=True)
        self.update_references()
        before = self.path(1).read_bytes()
        self.assertEqual(validate_dataset(self.root).status, "READY")
        self.assertEqual(self.path(1).read_bytes(), before)

    def test_wrong_explicit_mate_blocks(self):
        self.path(2).write_bytes(self.path(1).read_bytes())
        self.update_references()
        report, checks = self.checks()
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("Header mate is 1; expected 2", checks["FASTQ001:sequencing/SRR6357070_R2.fastq.gz"].message)
        self.assertEqual(checks["PAIR001:SRR6357070"].outcome, "NOT_RUN")

    def test_correct_counts_do_not_hide_reordered_pairs(self):
        self.write_reads(second_order=(3, 2, 1))
        self.update_references()
        report, checks = self.checks()
        self.assertEqual(checks["PAIR001:SRR6357070"].outcome, "FAIL")
        self.assertEqual(checks["SRC001"].outcome, "PASS")
        self.assertEqual(report.status, "BLOCKED")

    def test_missing_mate_has_one_root_blocker_and_three_skips(self):
        self.path(2).unlink()
        report, checks = self.checks()
        self.assertEqual(report.counts()["blocking_findings"], 1)
        self.assertEqual(report.counts()["required_checks_not_run"], 3)
        self.assertEqual(checks["SRC001"].blocked_by, ["FASTQ001:sequencing/SRR6357070_R2.fastq.gz"])

    def test_structurally_valid_byte_change_fails_source_reference(self):
        text = gzip.decompress(self.path(1).read_bytes()).replace(b"ACGTN", b"TCGTN", 1)
        self.path(1).write_bytes(gzip.compress(text, mtime=0))
        report, checks = self.checks()
        self.assertEqual(checks["FASTQ001:sequencing/SRR6357070_R1.fastq.gz"].outcome, "PASS")
        self.assertEqual(checks["SRC001"].outcome, "FAIL")
        self.assertEqual(report.status, "BLOCKED")

    def test_missing_extra_or_malformed_reference_is_blocked_by_configuration(self):
        original = copy.deepcopy(self.config)
        for mode in ("missing", "extra", "malformed"):
            with self.subTest(mode=mode):
                self.config = copy.deepcopy(original)
                references = self.config["integrity"]["reference_sha256"]
                if mode == "missing":
                    references.pop(next(iter(references)))
                elif mode == "extra":
                    references["sequencing/unexpected.fastq.gz"] = "0" * 64
                else:
                    references[next(iter(references))] = "not a checksum"
                self.write_config()
                report, checks = self.checks()
                self.assertEqual(checks["CFG001"].outcome, "FAIL")
                self.assertEqual(report.counts()["valid_fastq_files"], 0)

    def test_unsupported_profile_and_synthetic_flag_fail_closed(self):
        original = copy.deepcopy(self.config)
        for mode in ("profile", "synthetic", "legacy_fields", "provenance"):
            with self.subTest(mode=mode):
                self.config = copy.deepcopy(original)
                if mode == "profile":
                    self.config["fastq"]["header_profile"] = "auto"
                elif mode == "synthetic":
                    self.config["synthetic_data_only"] = True
                elif mode == "legacy_fields":
                    self.config["fastq"]["read_id_pattern"] = ".*"
                else:
                    self.config.pop("provenance")
                self.write_config()
                self.assertEqual(self.checks()[1]["CFG001"].outcome, "FAIL")

    def test_public_profile_cannot_be_enabled_inside_legacy_specification(self):
        legacy = json.loads((ROOT / "tests/fixtures/validation_requirements.json").read_text())
        legacy["fastq"]["header_profile"] = "slash_mate"
        self.assertEqual(_configuration(legacy).outcome, "FAIL")


class PublicPackagingTests(PublicFixture):
    def test_public_package_has_seven_files_and_retains_exact_sources(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        record = build_package(self.db, report.run_id, self.destination)
        self.assertEqual(record["files_count"], 7)
        self.assertEqual(verify_package(self.db, self.destination)["verification_status"], "VERIFIED")
        for mate in (1, 2):
            relative = self.path(mate).relative_to(self.root)
            self.assertEqual((self.destination / relative).read_bytes(), self.path(mate).read_bytes())
        packaged = json.loads((self.destination / "requirements.json").read_text())
        self.assertEqual(packaged["provenance"], self.config["provenance"])

    def test_public_report_identifies_subset_and_escapes_source_text(self):
        evidence = validate_dataset(self.root).to_dict()
        evidence["requirements"]["provenance"]["processing"] = "<script>unexpected</script>"
        html = render_report(evidence)
        self.assertIn("Public experimental subset", html)
        self.assertIn("SRR6357070", html)
        self.assertIn("&lt;script&gt;unexpected&lt;/script&gt;", html)
        self.assertNotIn("Synthetic demo", html)

    def test_changed_source_after_validation_refuses_package(self):
        report = validate_dataset(self.root)
        save_run(report, self.db)
        self.path(1).write_bytes(gzip.compress(b"changed"))
        with self.assertRaises(PackagingError):
            build_package(self.db, report.run_id, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list_packages(self.db), [])

    def test_public_evidence_cannot_claim_legacy_rules(self):
        evidence = validate_dataset(self.root).to_dict()
        evidence["rules_version"] = "1.0.0"
        with self.assertRaisesRegex(PackagingError, "Legacy rules"):
            _input_plan(evidence)

    def test_missing_source_check_cannot_authorize_packaging(self):
        evidence = validate_dataset(self.root).to_dict()
        evidence["checks"] = [c for c in evidence["checks"] if c["check_id"] != "SRC001"]
        evidence["expected_check_ids"].remove("SRC001")
        evidence["counts"]["passed_checks"] -= 1
        with self.assertRaises(PackagingError):
            _input_plan(evidence)

    def test_pass_label_cannot_hide_conflicting_source_fingerprints(self):
        evidence = validate_dataset(self.root).to_dict()
        key = "sequencing/SRR6357070_R1.fastq.gz"
        evidence["requirements"]["integrity"]["reference_sha256"][key] = "0" * 64
        with self.assertRaisesRegex(PackagingError, "source references"):
            _input_plan(evidence)

    def test_legacy_saved_run_and_package_remain_usable(self):
        config = json.loads((ROOT / "tests/fixtures/validation_requirements.json").read_text())
        config["expected_samples"] = ["SRR6357070"]
        (self.root / "requirements.json").write_text(json.dumps(config))
        (self.root / "samples.csv").write_text("sample_id,condition,tissue,notes\nSRR6357070,Control,Liver,Synthetic unit fixture\n")
        for mate in (1, 2):
            self.path(mate).write_bytes(gzip.compress(f"@READ_000001/{mate}\nACGT\n+\nIIII\n".encode(), mtime=0))
        with patch("biodataqc.results.RULES_VERSION", "1.0.0"):
            report = validate_dataset(self.root)
            before = report.to_dict()
            save_run(report, self.db)
        record = build_package(self.db, report.run_id, self.destination)
        self.assertEqual(get_run(self.db, report.run_id), before)
        self.assertEqual(record["files_count"], 7)
        self.assertEqual(verify_package(self.db, self.destination)["verification_status"], "VERIFIED")


class PreparationTests(PublicFixture):
    def setUp(self):
        super().setUp()
        self.output = self.base / "prepared/public"
        self.broken = self.base / "prepared/missing_mate"
        self.cache = self.base / "download_cache"
        self.catalog_patch = patch("biodataqc.public_data._catalog", return_value=copy.deepcopy(self.config))
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.data = {item["url"]: (self.root / item["local_path"]).read_bytes()
                     for item in self.config["provenance"]["files"]}

    def prepare(self):
        return prepare_public_demo(self.output, self.broken, self.cache, progress=lambda message: None)

    def request(self, request, timeout):
        self.assertEqual(timeout, 30)
        return io.BytesIO(self.data[request.full_url])

    def seed_cache(self):
        self.cache.mkdir()
        for item in self.config["provenance"]["files"]:
            (self.cache / item["original_name"]).write_bytes(self.data[item["url"]])

    def test_download_builds_ready_and_explicitly_faulted_copies_without_sql(self):
        with patch("biodataqc.public_data.urllib.request.urlopen", side_effect=self.request) as request:
            result = self.prepare()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["records_scanned"], 6)
        self.assertEqual(validate_dataset(self.output).status, "READY")
        broken = validate_dataset(self.broken)
        self.assertEqual(broken.status, "BLOCKED")
        self.assertEqual(broken.counts()["blocking_findings"], 1)
        self.assertTrue(broken.requirements["provenance"]["deliberate_faults"])
        self.assertFalse(self.db.exists())
        for item in self.config["provenance"]["files"]:
            self.assertEqual((self.output / item["local_path"]).read_bytes(), self.data[item["url"]])

    def test_verified_cache_is_reused_without_network(self):
        self.seed_cache()
        with patch("biodataqc.public_data.urllib.request.urlopen") as request:
            self.prepare()
        request.assert_not_called()

    def test_existing_destination_is_preserved_without_downloading(self):
        self.output.mkdir(parents=True)
        marker = self.output / "keep.txt"
        marker.write_text("retained")
        with patch("biodataqc.public_data.urllib.request.urlopen") as request:
            with self.assertRaisesRegex(PreparationError, "already exists"):
                self.prepare()
        request.assert_not_called()
        self.assertEqual(marker.read_text(), "retained")

    def test_overlapping_paths_are_refused(self):
        with self.assertRaisesRegex(PreparationError, "non-overlapping"):
            prepare_public_demo(self.output, self.output / "broken", self.cache, progress=lambda x: None)

    def test_corrupt_cache_is_preserved_and_refused(self):
        self.seed_cache()
        target = self.cache / self.config["provenance"]["files"][0]["original_name"]
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(PreparationError, "mismatch"):
            self.prepare()
        self.assertEqual(target.read_bytes(), b"corrupt")
        self.assertFalse(self.output.exists())

    def test_cached_symlink_is_refused(self):
        self.cache.mkdir()
        (self.cache / self.config["provenance"]["files"][0]["original_name"]).symlink_to(self.path(1))
        with self.assertRaisesRegex(PreparationError, "non-symlink"):
            self.prepare()

    def test_interrupted_download_removes_partial_files(self):
        with patch("biodataqc.public_data.urllib.request.urlopen", side_effect=OSError("network failure")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(list(self.cache.glob("*.gz")), [])
        self.assertEqual(list(self.cache.glob(".download-*")), [])
        self.assertFalse(self.output.exists())

    def test_oversized_download_is_refused_and_removed(self):
        first = next(iter(self.data.values()))
        with patch("biodataqc.public_data.urllib.request.urlopen", return_value=io.BytesIO(first + b"extra")):
            with self.assertRaisesRegex(PreparationError, "exceeded"):
                self.prepare()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_same_size_wrong_download_hash_is_refused(self):
        first = bytearray(next(iter(self.data.values())))
        first[-1] ^= 1
        with patch("biodataqc.public_data.urllib.request.urlopen", return_value=io.BytesIO(first)):
            with self.assertRaisesRegex(PreparationError, "checksum mismatch"):
                self.prepare()
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_failed_second_promotion_rolls_back_only_new_datasets(self):
        self.seed_cache()
        original = os.replace
        def fail_second(source, destination):
            if destination == self.broken:
                raise OSError("simulated second promotion failure")
            return original(source, destination)
        with patch("biodataqc.public_data.os.replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.broken.exists())
        self.assertEqual(len(list(self.cache.glob("*.gz"))), 2)
        self.assertEqual(list(self.output.parent.glob(".public-*")), [])

    def test_catalog_cannot_escape_source_destination_mapping(self):
        self.catalog_patch.stop()
        invalid = copy.deepcopy(self.config)
        invalid["provenance"]["files"][0]["local_path"] = "../escape.fastq.gz"
        with patch("biodataqc.public_data._catalog", return_value=invalid):
            with self.assertRaisesRegex(PreparationError, "mapping"):
                self.prepare()
        self.assertFalse(self.cache.exists())


class PublicInterfaceTests(PublicFixture):
    def start(self):
        source = f"from pathlib import Path\nfrom biodataqc.ui import main\nmain(Path({str(self.base)!r}))\n"
        app = AppTest.from_string(source, default_timeout=30).run()
        self.assertEqual([e.message for e in app.exception], [])
        return app

    def test_public_data_runs_reports_packages_and_history_share_evidence(self):
        app = self.start()
        app.radio(key="bqc_source").set_value("Public yeast").run()
        app.button(key="bqc_run").click().run()
        self.assertEqual([e.message for e in app.exception], [])
        evidence = app.session_state["bqc_evidence"]
        self.assertEqual(evidence["status"], "READY")
        self.assertIn("Public experimental subset", " ".join(c.value for c in app.caption))
        app.button(key="bqc_save_reports").click().run()
        app.button(key="bqc_build_package").click().run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertEqual(app.session_state["bqc_package_notice"]["files_count"], 7)
        app.button(key="bqc_open_history").click().run()
        self.assertEqual(app.radio(key="bqc_source").value, "Public yeast")
        self.assertEqual(app.session_state["bqc_evidence"]["run_id"], evidence["run_id"])
        self.assertEqual(len(list_runs(self.db)), 1)

    def test_missing_mate_choice_is_explicitly_faulted_and_cannot_package(self):
        broken = self.base / "data/public_yeast_missing_mate"
        shutil.copytree(self.root, broken)
        (broken / "sequencing/SRR6357070_R2.fastq.gz").unlink()
        self.config["provenance"]["deliberate_faults"] = ["Removed R2 for this unit test"]
        (broken / "requirements.json").write_text(json.dumps(self.config))
        app = self.start()
        app.radio(key="bqc_source").set_value("Public yeast · missing mate").run()
        app.button(key="bqc_run").click().run()
        self.assertEqual([e.message for e in app.exception], [])
        self.assertEqual(app.session_state["bqc_evidence"]["status"], "BLOCKED")
        self.assertTrue(app.button(key="bqc_build_package").disabled)
        self.assertIn("Deliberate test modifications", " ".join(w.value for w in app.warning))


if __name__ == "__main__":
    unittest.main()
