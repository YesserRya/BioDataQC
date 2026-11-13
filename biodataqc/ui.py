"""Streamlit interface. Side effects only run after explicit action buttons."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from . import ui_service as service
from .appearance import apply_style, render_header
from .comparison import compare_runs
from .fastqc_ui import render_quality
from .history import get_run, list_packages, list_runs
from .inputs import load_dataset
from .packaging import verify_package
from .results import utc_now


DATASETS = {"Clean demo": "data/clean", "Broken demo": "data/broken",
            "Public yeast": "data/public_yeast", "Public yeast · missing mate": "data/public_yeast_missing_mate"}


_VIEW_KEYS = (
    "bqc_evidence", "bqc_inventory", "bqc_saved", "bqc_storage_error", "bqc_error",
    "bqc_freshness", "bqc_owner", "bqc_next_update", "bqc_package_name",
    "bqc_report_notice", "bqc_package_notice", "bqc_package_verification", "bqc_package_error",
    "bqc_package_selection", "bqc_history_selection", "bqc_outcomes", "bqc_origin",
    "bqc_compare_newer", "bqc_compare_baseline", "bqc_compare_other_folders",
    "bqc_comparison", "bqc_comparison_error", "bqc_comparison_signature",
    "bqc_compare_all_checks", "bqc_compare_all_files", "bqc_compare_check_detail",
    "bqc_fastqc_error", "bqc_fastqc_selection", "bqc_fastqc_file", "bqc_fastqc_notice",
)


def _clear_view():
    for key in _VIEW_KEYS:
        st.session_state.pop(key, None)


def _activate(evidence, *, saved, storage_error=None, origin="validation"):
    _clear_view()
    st.session_state["bqc_evidence"] = evidence
    st.session_state["bqc_saved"] = saved
    st.session_state["bqc_storage_error"] = storage_error
    st.session_state["bqc_origin"] = origin
    st.session_state["bqc_package_name"] = service.default_package_name(evidence)
    if origin == "validation" and evidence["status"] == "READY":
        st.session_state["bqc_freshness"] = {"state": "MATCHED", "checked_at": evidence["finished_at"],
                                           "message": "Input snapshot was checked during this validation run."}
    else:
        st.session_state["bqc_freshness"] = {"state": "UNCHECKED", "checked_at": None,
                                           "message": "Current source files have not been compared with this saved run in this session."}


def _open_pending_run(project_root):
    pending = st.session_state.pop("bqc_pending_run", None)
    if pending is None:
        return
    try:
        evidence = get_run(pending["database"], pending["run_id"])
        source = Path(evidence["dataset_path"])
        demo_paths = {label: project_root / path for label, path in DATASETS.items()}
        choice = next((label for label, path in demo_paths.items() if path.resolve() == source.resolve()), "Custom folder")
        st.session_state["bqc_source"] = choice
        st.session_state["bqc_custom_path"] = str(source)
        st.session_state["bqc_database"] = str(pending["database"])
        st.session_state["bqc_context"] = (str(source.resolve()), str(Path(pending["database"]).resolve()))
        _activate(evidence, saved=True, origin="history")
    except Exception as error:
        _clear_view()
        st.session_state["bqc_error"] = f"Could not open the saved run: {error}"


def _frame(rows, *, columns=None):
    frame = pd.DataFrame(rows, columns=columns)
    if frame.empty:
        st.caption("No rows to display.")
    else:
        st.dataframe(frame, hide_index=True, width="stretch")


def _delivery(evidence):
    if evidence is None:
        preview = st.session_state.get("bqc_inventory")
        if preview:
            st.info("Inventory loaded · NOT_VALIDATED. Run validation to check metadata, FASTQ content and pairs.")
            summary = preview["summary"]
            columns = st.columns(4)
            for column, (label, value) in zip(columns, [
                ("Expected samples", summary["expected_samples"]),
                ("Files present", f"{summary['present_expected_files']} / {summary['expected_files']}"),
                ("Missing files", summary["missing_expected_files"]),
                ("Unexpected entries", summary["unexpected_entries"]),
            ]):
                column.metric(label, value)
            _frame(preview["inventory"])
        else:
            st.info("Choose a dataset in the sidebar. Load its inventory or run validation to begin.")
            st.markdown("**Workflow:** inspect inputs → validate and save → review findings → prepare a verified package.")
        return
    summary = service.delivery_summary(evidence)
    freshness = st.session_state.get("bqc_freshness", {})
    if freshness.get("state") in {"REVALIDATE", "UNAVAILABLE"}:
        st.warning("Revalidation required · This run cannot currently support packaging.")
        st.caption(f"Saved validation status: {evidence['status']}")
    else:
        notice = {"READY": st.success, "BLOCKED": st.error, "INCOMPLETE": st.warning}[evidence["status"]]
        label = "Saved run" if st.session_state.get("bqc_saved") else "Validation result (not stored)"
        notice(f"{label}: {evidence['status']} · {summary['title']}")
    counts, inventory = evidence["counts"], evidence.get("inventory_summary", {})
    columns = st.columns(4)
    for column, (label, value) in zip(columns, [
        ("Expected samples", len(evidence["requirements"].get("expected_samples", []))),
        ("Files present", f"{inventory.get('present_expected_files', 0)} / {inventory.get('expected_files', 0)}"),
        ("Blocking findings", counts["blocking_findings"]), ("Warnings", counts["warnings"]),
    ]):
        column.metric(label, value)
    st.caption(f"{counts['records_scanned']:,} records scanned · {counts['valid_fastq_files']} structurally valid FASTQ files · "
               f"{counts['required_checks_not_run']} required checks not run · {counts['execution_errors']} execution errors")
    with st.container(border=True):
        st.subheader("Delivery summary")
        if freshness.get("state") in {"REVALIDATE", "UNAVAILABLE"}:
            st.write("The saved decision cannot currently authorize packaging for this dataset.")
            st.write("Next action: Review the snapshot message, resolve input changes or access issues, then run validation again.")
        else:
            st.write(summary["impact"])
            st.write("Next action: " + summary["next_action"])
        named = ", ".join(summary["named_samples"]) or "None named in findings"
        st.write("Samples named in findings: " + named)
        if summary["dataset_level_findings"]:
            st.caption("Dataset-level findings also require review; the sample list is not an exhaustive scope assessment.")
        if summary["findings"]:
            _frame([{"Severity": c["severity"], "Sample": c.get("sample_id") or "Dataset",
                     "Finding": c["message"], "Next action": c.get("next_action") or "Review evidence"}
                    for c in summary["findings"]])
    with st.container(border=True):
        st.subheader("Current input snapshot")
        st.caption("A validation result describes its validation time. Files are checked again whenever a package is built.")
        if st.button("Check current inputs", key="bqc_check_snapshot", disabled=evidence["status"] != "READY"):
            with st.spinner("Comparing current inventory and fingerprints…"):
                try:
                    st.session_state["bqc_freshness"] = service.check_snapshot(evidence)
                except Exception as error:
                    st.session_state["bqc_freshness"] = {"state": "UNAVAILABLE", "checked_at": utc_now(),
                                                         "message": f"The snapshot check could not complete: {error}"}
            st.rerun()
        freshness = st.session_state.get("bqc_freshness", {})
        if freshness.get("state") == "MATCHED":
            st.success(freshness["message"])
        elif freshness.get("state") in {"REVALIDATE", "UNAVAILABLE"}:
            st.warning(freshness["message"])
        else:
            st.info(freshness.get("message", "No current comparison is available."))
        if freshness.get("checked_at"):
            st.caption("Last comparison: " + service.display_time(freshness["checked_at"]))


def _technical(evidence):
    if evidence is None:
        preview = st.session_state.get("bqc_inventory")
        if preview:
            st.subheader("Inventory preview")
            _frame(preview["inventory"])
            with st.expander("Sample metadata"):
                _frame(preview["metadata_rows"], columns=preview["metadata_columns"])
            with st.expander("Applied requirements"):
                st.json(preview["requirements"])
        else:
            st.info("Load an inventory or run validation to inspect technical evidence.")
        return
    st.subheader("Check results")
    st.caption("Dependent checks marked NOT_RUN are retained separately from root findings.")
    outcomes = st.multiselect("Show outcomes", ["FAIL", "ERROR", "NOT_RUN", "PASS"],
                              default=["FAIL", "ERROR", "NOT_RUN"], key="bqc_outcomes")
    rows = []
    for check in evidence["checks"]:
        if check["outcome"] in outcomes:
            rows.append({"Rule": check["rule_id"], "Outcome": check["outcome"], "Severity": check["severity"] or "—",
                         "Required": check["required"], "Sample": check["sample_id"] or "—",
                         "File": check["file_path"] or "—", "Record": check["record_number"],
                         "Message": check["message"], "Next action": check["next_action"] or "—",
                         "Depends on": ", ".join(check["blocked_by"]) or "—"})
    st.caption(f"Showing {len(rows)} of {len(evidence['checks'])} recorded checks.")
    _frame(rows)
    with st.expander("File inventory"):
        _frame(evidence["inventory"])
    with st.expander("Sample metadata · literal values"):
        _frame(evidence["metadata_rows"], columns=evidence["metadata_columns"])
    with st.expander("Input fingerprints"):
        _frame([{"Relative path": path, "SHA-256": digest} for path, digest in sorted(evidence["input_sha256"].items())])
    with st.expander("Applied requirements"):
        st.json(evidence["requirements"])
    with st.expander("Complete run evidence"):
        st.json(evidence)


def _reports_and_packages(evidence, database, project_root):
    if evidence is None:
        st.info("Run validation or open a saved run to create reports and packages.")
        return
    saved = st.session_state.get("bqc_saved", False)
    st.subheader("Report preparation")
    left, right = st.columns(2)
    owner = left.text_input("Responsible person / team", key="bqc_owner", placeholder="Not assigned")
    update = right.text_input("Next update", key="bqc_next_update", placeholder="To be confirmed")
    st.caption("These fields are supplied by you. They do not change the recorded validation decision.")
    downloads = service.report_downloads(evidence, owner=owner, next_update=update)
    signature = (evidence["run_id"], owner, update)
    columns = st.columns(3)
    save_clicked = columns[0].button("Save report files", key="bqc_save_reports", disabled=not saved, width="stretch")
    columns[1].download_button("Download HTML report", data=downloads["html"],
                               file_name=downloads["stem"] + ".html", mime="text/html",
                               key="bqc_download_html", disabled=not saved, width="stretch")
    columns[2].download_button("Download validation JSON", data=downloads["json"],
                               file_name=downloads["stem"] + ".json", mime="application/json",
                               key="bqc_download_json", width="stretch")
    if save_clicked:
        try:
            paths = service.save_report_files(evidence, database, project_root, owner=owner, next_update=update)
            st.session_state["bqc_report_notice"] = {"signature": signature, "paths": paths}
        except Exception as error:
            st.error(f"Report saving failed: {error}")
    notice = st.session_state.get("bqc_report_notice")
    if notice and notice["signature"] == signature:
        st.success("HTML and JSON reports saved for this run.")
        st.code("\n".join(notice["paths"].values()), language=None)
    if not saved:
        st.warning("This result has not been stored in SQL. Download its JSON if needed, then resolve the history-save problem and validate again.")
    st.divider()
    st.subheader("Verified local package")
    folder = st.text_input("New package folder name", key="bqc_package_name")
    destination, path_error = None, None
    try:
        destination = service.package_destination(project_root, folder)
        if destination.exists() or destination.is_symlink():
            path_error = "This folder already exists. Choose a new name to build another package."
    except ValueError as error:
        path_error = str(error)
    if destination is not None:
        st.caption("Destination: " + str(destination))
    freshness = st.session_state.get("bqc_freshness", {})
    allowed = saved and evidence["status"] == "READY" and freshness.get("state") not in {"REVALIDATE", "UNAVAILABLE"}
    if not allowed:
        st.info("Packaging needs a saved READY run and matching current inputs. Resolve blockers or revalidate changed inputs first.")
    if path_error:
        st.caption(path_error)
    if st.button("Build verified package", key="bqc_build_package", type="primary",
                  disabled=not allowed or path_error is not None):
        try:
            with st.spinner("Checking inputs, staging copies and verifying the package…"):
                record = service.create_package(evidence, database, project_root, folder, owner=owner, next_update=update)
            st.session_state["bqc_package_notice"] = record
            st.session_state["bqc_package_error"] = None
            st.session_state["bqc_package_verification"] = None
            st.rerun()
        except Exception as error:
            st.session_state["bqc_package_error"] = f"Last package preparation failed: {error}"
            try:
                st.session_state["bqc_freshness"] = service.check_snapshot(evidence)
            except Exception:
                st.session_state["bqc_freshness"] = {"state": "UNAVAILABLE", "checked_at": utc_now(),
                                                     "message": "The current input snapshot could not be established. Revalidate before retrying."}
            st.rerun()
    if st.session_state.get("bqc_package_error"):
        st.error(st.session_state["bqc_package_error"])
    record = st.session_state.get("bqc_package_notice")
    if record and record["run_id"] == evidence["run_id"]:
        st.success(f"Package completed · {record['files_count']} files, including the manifest.")
        st.code(record["package_path"], language=None)
    st.caption("Packages are prepared locally. Input files are retained, and every build rechecks the saved snapshot in the backend.")
    if not saved:
        return
    try:
        packages = list_packages(database, run_id=evidence["run_id"], limit=100)
    except Exception as error:
        st.error(f"Package history could not be read: {error}")
        return
    st.subheader("Packages linked to this run")
    if not packages:
        st.caption("No completed packages are recorded for this run.")
        return
    by_id = {p["package_id"]: p for p in packages}
    if st.session_state.get("bqc_package_selection") not in by_id:
        st.session_state["bqc_package_selection"] = packages[0]["package_id"]
    selected = st.selectbox("Select a package", options=list(by_id), key="bqc_package_selection",
                            format_func=lambda key: f"{Path(by_id[key]['package_path']).name} · {service.display_time(by_id[key]['completed_at'])}")
    selected_package = by_id[selected]
    st.code(selected_package["package_path"], language=None)
    st.caption(f"{selected_package['files_count']} files · {selected_package['total_bytes']:,} bytes · Package ID {selected}")
    if st.button("Verify selected package", key="bqc_verify_package"):
        try:
            with st.spinner("Verifying the manifest and package contents…"):
                verify_package(database, selected_package["package_path"])
            st.session_state["bqc_package_verification"] = {"package_id": selected, "ok": True,
                                                           "checked_at": utc_now(), "message": "Package verification passed."}
        except Exception as error:
            st.session_state["bqc_package_verification"] = {"package_id": selected, "ok": False,
                                                           "checked_at": utc_now(), "message": str(error)}
    result = st.session_state.get("bqc_package_verification")
    if result and result["package_id"] == selected:
        (st.success if result["ok"] else st.error)(result["message"])
        st.caption("Checked: " + service.display_time(result["checked_at"]))
    st.caption("Completion records describe creation time; verification above describes its displayed check time.")
    with st.expander("Recorded manifest SHA-256"):
        st.code(selected_package["manifest_sha256"], language=None)


def _comparison_result(result):
    before, after = result["before"], result["after"]
    st.subheader(f"Saved readiness: {before['status']} → {after['status']}")
    if result["comparable"]:
        st.success("The runs use the same recorded requirements and validation rules, with stable input snapshots.")
    else:
        st.warning("Comparison is limited. Passing results are not treated as resolved failures.")
        for reason in result["reasons"]:
            st.write("• " + reason)
    for note in result["notes"]:
        st.caption(note)
    columns = st.columns(4)
    for column, key, title in zip(columns, ("RESOLVED", "NEW_FAILURE", "STILL_FAILING", "UNASSESSED"),
                                  ("Resolved checks", "New failures", "Still failing", "Unassessed checks")):
        column.metric(title, result["counts"][key])
    st.caption(f"Newly assessed failures: {result['counts']['NEWLY_ASSESSED_FAILURE']} · "
               f"Previously unassessed checks now passing: {result['counts']['NOW_PASSES']} · "
               f"Checks not comparable: {result['counts']['NOT_COMPARABLE']}")
    with st.expander("Run totals and identity"):
        st.json({"baseline": before, "newer": after})
    if result["requirements_changes"]:
        with st.expander("Changed requirements", expanded=True):
            _frame([{"Field": row["field"],
                     "Baseline": json.dumps(row["before"], ensure_ascii=False) if row["before_present"] else "Not recorded",
                     "Newer": json.dumps(row["after"], ensure_ascii=False) if row["after_present"] else "Not recorded"}
                    for row in result["requirements_changes"]])
    st.subheader("Check changes")
    all_checks = st.checkbox("Show unchanged passing and optional checks", key="bqc_compare_all_checks")
    visible = [row for row in result["checks"] if all_checks or row["change"] not in {"STILL_PASSING", "OPTIONAL_NOT_RUN"}]

    def outcome(check):
        if check is None:
            return "Not recorded"
        return check["outcome"] + (" / " + check["severity"] if check.get("severity") else "")

    _frame([{"Change": row["label"], "Check": row["check_id"],
             "Location": row["file_path"] or row["sample_id"] or "Dataset",
             "Baseline": outcome(row["before"]), "Newer": outcome(row["after"]),
             "Explanation": row["explanation"]} for row in visible])
    if visible:
        with st.expander("Inspect check messages and evidence"):
            by_id = {row["check_id"]: row for row in visible}
            if st.session_state.get("bqc_compare_check_detail") not in by_id:
                st.session_state["bqc_compare_check_detail"] = next(iter(by_id))
            selected = st.selectbox("Check to inspect", list(by_id), key="bqc_compare_check_detail")
            item = by_id[selected]
            st.json({"baseline": item["before"], "newer": item["after"]})
    st.subheader("File changes")
    st.caption("SHA-256 comparisons refer to recorded file bytes (compressed bytes for FASTQ.gz). "
               "Missing fingerprints are unverified, even when file sizes match.")
    all_files = st.checkbox("Show unchanged files", key="bqc_compare_all_files")
    visible_files = [row for row in result["files"] if all_files or row["change"] != "UNCHANGED" or row["inventory_changed"]]
    _frame([{"File": row["relative_path"], "Change": row["change"].replace("_", " ").capitalize(),
             "Earlier inventory": row["before"]["status"], "Newer inventory": row["after"]["status"],
             "Earlier bytes": row["before"]["size_bytes"], "Newer bytes": row["after"]["size_bytes"],
             "Inventory changed": row["inventory_changed"]} for row in visible_files])
    with st.expander("Full file fingerprints"):
        st.json(result["files"])
    st.download_button("Download comparison JSON", json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                       file_name="biodataqc_comparison.json", mime="application/json", key="bqc_download_comparison")


def _compare_history(database, rows):
    st.divider()
    st.subheader("Compare validation runs")
    st.caption("Select two saved observations of the same project. Comparing does not add a validation run.")
    choices = sorted([r for r in rows if r["project_id"]],
                     key=lambda r: datetime.fromisoformat(r["finished_at"]), reverse=True)
    if len(choices) < 2:
        st.info("Save at least two runs for a project to compare them. Validate again after correcting the dataset.")
        return
    lookup = {row["run_id"]: row for row in choices}

    def label(key):
        row = lookup[key]
        return (f"{row['project_id']} · {Path(row['dataset_path']).name} · {row['status']} · "
                f"{service.display_time(row['finished_at'])} · {key[:8]}")

    if st.session_state.get("bqc_compare_newer") not in lookup:
        st.session_state["bqc_compare_newer"] = choices[0]["run_id"]
    newer_id = st.selectbox("Newer run", list(lookup), format_func=label, key="bqc_compare_newer")
    newer = lookup[newer_id]
    other_folders = st.checkbox("Include other folders in this project", key="bqc_compare_other_folders")
    candidates = [r["run_id"] for r in choices if r["run_id"] != newer_id and r["project_id"] == newer["project_id"]
                  and datetime.fromisoformat(r["finished_at"]) <= datetime.fromisoformat(newer["finished_at"])
                  and (other_folders or r["dataset_path"] == newer["dataset_path"])]
    if not candidates:
        st.session_state.pop("bqc_comparison", None)
        st.info("No earlier run matches this project and folder in the displayed history. "
                "Choose a later run, include other folders, or validate this dataset again. "
                "Enable Show all datasets above to expand the history list.")
        return
    if st.session_state.get("bqc_compare_baseline") not in candidates:
        st.session_state["bqc_compare_baseline"] = candidates[0]
    baseline_id = st.selectbox("Baseline run", candidates, format_func=label, key="bqc_compare_baseline")
    if lookup[baseline_id]["dataset_path"] != newer["dataset_path"]:
        st.info("You selected different folders. Confirm they represent the snapshots you intend to compare.")
    signature = (str(database), baseline_id, newer_id)
    if st.session_state.get("bqc_comparison_signature") != signature:
        st.session_state.pop("bqc_comparison", None)
        st.session_state.pop("bqc_comparison_error", None)
        st.session_state["bqc_comparison_signature"] = signature
    if st.button("Compare selected runs", key="bqc_compare_runs", type="primary"):
        st.session_state.pop("bqc_comparison", None)
        st.session_state.pop("bqc_comparison_error", None)
        try:
            st.session_state["bqc_comparison"] = compare_runs(get_run(database, baseline_id), get_run(database, newer_id))
        except Exception as error:
            st.session_state["bqc_comparison_error"] = f"Comparison could not be completed: {error}"
    if st.session_state.get("bqc_comparison_error"):
        st.error(st.session_state["bqc_comparison_error"])
    if st.session_state.get("bqc_comparison"):
        _comparison_result(st.session_state["bqc_comparison"])


def _history(database, dataset):
    st.subheader("Validation history")
    show_all = st.checkbox("Show all datasets", value=False, key="bqc_all_history")
    if not database.exists():
        st.info("No history database yet. Your first validation will create it automatically.")
        return
    try:
        rows = list_runs(database, dataset_path=None if show_all else dataset, limit=200)
    except Exception as error:
        st.error(f"History could not be read: {error}")
        return
    if not rows:
        st.info("No saved runs match this dataset. Enable Show all datasets to browse the other runs.")
        return
    _frame([{"Finished (Paris time)": service.display_time(r["finished_at"]), "Project": r["project_id"],
             "Dataset": r["dataset_path"], "Status": r["status"], "Blockers": r["blocking_findings"],
             "Warnings": r["warnings"], "Run ID": r["run_id"]} for r in rows])
    st.caption(f"Showing the latest {len(rows)} matching run(s), newest stored first. Statuses are historical.")
    lookup = {r["run_id"]: r for r in rows}
    if st.session_state.get("bqc_history_selection") not in lookup:
        st.session_state["bqc_history_selection"] = rows[0]["run_id"]
    run_id = st.selectbox("Select a saved run", options=list(lookup), key="bqc_history_selection",
                          format_func=lambda key: f"{lookup[key]['status']} · {Path(lookup[key]['dataset_path']).name} · {service.display_time(lookup[key]['finished_at'])} · {key[:8]}")
    if st.button("Open selected run", key="bqc_open_history"):
        st.session_state["bqc_pending_run"] = {"database": str(database), "run_id": run_id}
        st.rerun()
    _compare_history(database, rows)


def main(project_root: Path):
    project_root = project_root.expanduser().resolve()
    st.set_page_config(page_title="BioDataQC · Delivery readiness", page_icon="🧬", layout="wide")
    apply_style()
    _open_pending_run(project_root)
    with st.sidebar:
        st.title("BioDataQC")
        st.caption("Local delivery quality workspace")
        source = st.radio("Dataset source", [*DATASETS, "Custom folder"], key="bqc_source")
        if source == "Custom folder":
            raw_dataset = st.text_input("Dataset folder", key="bqc_custom_path", placeholder="data/my_dataset or an absolute path")
        else:
            raw_dataset = DATASETS[source]
            if source.startswith("Public yeast") and not (project_root / raw_dataset).exists():
                st.caption("Prepare this dataset first: python scripts/prepare_public_demo.py")
        with st.expander("Workspace settings"):
            if "bqc_database" not in st.session_state:
                st.session_state["bqc_database"] = str(project_root / "outputs/biodataqc.sqlite")
            raw_database = st.text_input("SQLite history file", key="bqc_database")
            st.caption("Relative paths are resolved from the folder containing app.py. Display times use Europe/Paris.")
        try:
            dataset = service.resolve_path(raw_dataset, project_root)
            database = service.resolve_path(raw_database, project_root)
            context = (str(dataset), str(database))
            invalid_path = None
        except (ValueError, OSError) as error:
            context, invalid_path = None, str(error)
        if context != st.session_state.get("bqc_context"):
            _clear_view()
            st.session_state["bqc_context"] = context
        if invalid_path:
            st.error(invalid_path)
        else:
            st.caption("Selected folder")
            st.code(str(dataset), language=None)
        load_clicked = st.button("Load inventory", key="bqc_load", disabled=invalid_path is not None, width="stretch")
        run_clicked = st.button("Run validation", key="bqc_run", type="primary", disabled=invalid_path is not None, width="stretch")
        st.caption("Each validation creates a new SQL run. Inventory loading is optional and does not establish readiness.")
        with st.expander("Appearance"):
            animate_dna = st.toggle("Animate DNA", value=True, key="bqc_animate_dna",
                                    help="Pause the decorative helix. Your device's reduced-motion preference is always respected.")
    render_header(animate=animate_dna)
    if invalid_path:
        st.info("Enter valid local paths in the sidebar to continue.")
        return
    if load_clicked:
        _clear_view()
        try:
            with st.spinner("Loading metadata and file inventory…"):
                st.session_state["bqc_inventory"] = load_dataset(dataset).to_dict()
        except Exception as error:
            st.session_state["bqc_error"] = f"Inventory loading failed: {error}. You can still run validation to record the input failure."
    if run_clicked:
        _clear_view()
        try:
            with st.spinner("Scanning all records, comparing mates and saving the run…"):
                result = service.run_and_store(dataset, database)
            _activate(result["evidence"], saved=result["saved"], storage_error=result["storage_error"])
        except Exception as error:
            st.session_state["bqc_error"] = f"Validation could not complete: {error}"
    if st.session_state.get("bqc_error"):
        st.error(st.session_state["bqc_error"])
    evidence = st.session_state.get("bqc_evidence")
    if evidence:
        st.caption("Data profile: " + service.data_origin_label(evidence["requirements"]))
        st.caption(f"Project: {evidence.get('project_id') or 'Unavailable'} · Client: {evidence['requirements'].get('client_label', 'Not specified')}")
        st.caption(f"Selected run: {evidence['run_id']} · Finished {service.display_time(evidence['finished_at'])}")
        if st.session_state.get("bqc_saved"):
            st.caption("Evidence is stored in SQL history. This screen shows the selected run; it does not continuously monitor input files.")
        else:
            st.error("SQL history save failed: " + str(st.session_state.get("bqc_storage_error")))
        provenance = evidence["requirements"].get("provenance")
        if isinstance(provenance, dict):
            faults = provenance.get("deliberate_faults")
            if isinstance(faults, list) and faults:
                st.warning("Deliberate test modifications: " + "; ".join(map(str, faults)))
            with st.expander("Recorded data source"):
                st.write("Run accession: " + str(provenance.get("run_accession", "Not recorded")))
                st.write("Organism: " + str(provenance.get("organism", "Not recorded")))
                st.write(str(provenance.get("processing", "")))
                st.caption(str(provenance.get("checksum_scope", "")))
                st.json(provenance)
    delivery, technical, quality, reports, history = st.tabs(["Delivery summary", "Technical results", "Sequencing quality", "Reports & packages", "History"])
    with delivery:
        _delivery(evidence)
    with technical:
        _technical(evidence)
    with quality:
        render_quality(evidence, database, project_root, saved=st.session_state.get("bqc_saved", False))
    with reports:
        _reports_and_packages(evidence, database, project_root)
    with history:
        _history(database, dataset)
    st.divider()
    st.caption("BioDataQC · Local data-quality prototype · Shared validation rules, versioned run evidence and verified copy consistency.")
