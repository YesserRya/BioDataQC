"""Optional sequencing-quality view; explicit execution and saved artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from . import fastqc
from .ui_service import check_snapshot, display_time


def render_quality(evidence, database, project_root, *, saved):
    st.subheader("Sequencing quality · FastQC")
    st.caption("FastQC flags describe sequencing-quality observations. They are separate from structural READY/BLOCKED decisions "
               "and do not automatically change package eligibility. Interpret them in the context of the experiment.")
    with st.expander("FastQC setup"):
        executable = st.text_input("FastQC executable", value="fastqc", key="bqc_fastqc_executable",
                                   help="Executable name on PATH, or a full path. Launch Streamlit from the Conda environment containing FastQC.")
        st.code("conda install --override-channels --strict-channel-priority -c conda-forge -c bioconda fastqc=0.12.1", language="bash")
        if st.button("Check FastQC installation", key="bqc_fastqc_check"):
            try:
                tool = fastqc.check_installation(executable)
                st.session_state["bqc_fastqc_installation"] = {"executable": executable, "tool": tool}
            except Exception as error:
                st.session_state["bqc_fastqc_installation"] = {"executable": executable, "error": str(error)}
        installation = st.session_state.get("bqc_fastqc_installation", {})
        if installation.get("executable") == executable:
            if "error" in installation:
                st.error(installation["error"])
            elif "tool" in installation:
                st.success("FastQC " + installation["tool"]["version"] + " is available.")
                st.caption(installation["tool"]["executable"])
    if evidence is None:
        st.info("Run validation or open a saved run from History to analyze its sequencing quality.")
        return
    allowed = saved and evidence["status"] == "READY" and st.session_state.get("bqc_freshness", {}).get("state") not in {"REVALIDATE", "UNAVAILABLE"}
    if not allowed:
        st.info("This integration needs a saved READY run with matching current inputs. Resolve structural blockers or changed inputs and validate again.")
    output_root = project_root / "outputs/fastqc"
    st.caption("Analyzes all expected FASTQ files using verified temporary copies. Two files run in parallel, "
               "with a 10-minute execution timeout. Reports are retained under outputs/fastqc/.")
    if st.button("Run FastQC", key="bqc_fastqc_run", type="primary", disabled=not allowed):
        st.session_state.pop("bqc_fastqc_error", None)
        st.session_state.pop("bqc_fastqc_notice", None)
        try:
            with st.spinner("Checking the saved snapshot, running FastQC and verifying reports…"):
                directory = fastqc.run_fastqc(database, evidence["run_id"], output_root, executable=executable)
            st.session_state["bqc_fastqc_selection"] = directory.name
            st.session_state["bqc_fastqc_notice"] = directory.name
        except Exception as error:
            st.session_state["bqc_fastqc_error"] = str(error)
            try:
                st.session_state["bqc_freshness"] = check_snapshot(evidence)
            except Exception:
                pass
        st.rerun()
    if st.session_state.get("bqc_fastqc_error"):
        st.error("Last FastQC attempt failed: " + st.session_state["bqc_fastqc_error"])
        st.caption("Any completed analyses below are earlier saved results, not results of the failed attempt.")
    try:
        rows, errors = fastqc.list_analyses(output_root, evidence)
        for error in errors:
            st.warning("A saved analysis could not be listed: " + error)
    except Exception as error:
        st.error(f"FastQC history could not be read: {error}")
        return
    if not rows:
        st.info("No completed FastQC analysis is saved for this validation run yet.")
        return
    lookup = {r["analysis_id"]: r for r in rows}
    if st.session_state.get("bqc_fastqc_selection") not in lookup:
        st.session_state["bqc_fastqc_selection"] = rows[0]["analysis_id"]
    selected = st.selectbox("Saved FastQC analysis", list(lookup), key="bqc_fastqc_selection",
                            format_func=lambda key: f"{display_time(lookup[key]['finished_at'])} · v{lookup[key]['version']} · {key[:8]}")
    directory = Path(lookup[selected]["directory"])
    try:
        analysis = fastqc.load_analysis(directory, evidence)
    except Exception as error:
        st.error(f"Saved FastQC reports could not be verified: {error}")
        return
    if st.session_state.get("bqc_fastqc_notice") == selected:
        st.success("FastQC execution completed and reports were saved for this validation run.")
    st.caption(f"Analysis: {selected} · Validation run: {analysis['validation_run_id']} · "
               f"Completed {display_time(analysis['finished_at'])}. These are historical results.")
    totals = {state: sum(f["result"]["module_counts"][state] for f in analysis["files"]) for state in ("PASS", "WARN", "FAIL")}
    columns = st.columns(3)
    columns[0].metric("FASTQ files analyzed", len(analysis["files"]))
    columns[1].metric("FastQC WARN modules", totals["WARN"])
    columns[2].metric("FastQC FAIL modules", totals["FAIL"])
    if totals["WARN"] or totals["FAIL"]:
        st.warning("FastQC has flagged modules for review. The saved structural validation status remains " + evidence["status"] + ".")
    else:
        st.info("No WARN or FAIL flags were emitted by the modules FastQC ran. This is not a biological acceptance decision.")
    st.dataframe(pd.DataFrame([{"File": f["relative_path"], "Sample": f["sample_id"], "Mate": f["read"],
                                "Records": f["result"]["records"], "GC (%)": f["result"]["gc_percent"],
                                "Read length": f["result"]["basic"].get("Sequence length"),
                                "Encoding (FastQC)": f["result"]["basic"].get("Encoding"),
                                **f["result"]["module_counts"]} for f in analysis["files"]]), hide_index=True, width="stretch")
    by_file = {f["relative_path"]: f for f in analysis["files"]}
    if st.session_state.get("bqc_fastqc_file") not in by_file:
        st.session_state["bqc_fastqc_file"] = next(iter(by_file))
    choice = st.selectbox("FASTQ file to inspect", list(by_file), key="bqc_fastqc_file")
    item = by_file[choice]
    result = item["result"]
    st.dataframe(pd.DataFrame([{"Module": name, "FastQC status": module["status"]} for name, module in result["modules"].items()]),
                 hide_index=True, width="stretch")
    st.caption(f"The original report calls this temporary input {item['input_name']}; the mapping above links it to {choice}.")
    plots = result["plots"]
    st.markdown("**Quality along the read**")
    if plots["quality"]:
        st.line_chart(pd.DataFrame(plots["quality"]), x="Position", y=["Mean", "Median", "Lower quartile"],
                      x_label="Base position (group midpoint)", y_label="Quality score")
        st.caption("FastQC may group adjacent bases. Scores use its inferred encoding; "
                   "the BioDataQC input specification declares Phred+33. Check the inferred encoding above. "
                   "Use the original HTML report for the complete box-and-whisker plot.")
    else:
        st.caption("This module produced no plot data.")
    left, right = st.columns(2)
    with left:
        st.markdown("**GC distribution across reads**")
        if plots["gc"]:
            st.line_chart(pd.DataFrame(plots["gc"]), x="GC (%)", y="Count", x_label="GC content (%)", y_label="FastQC count")
        else:
            st.caption("This module produced no plot data.")
    with right:
        st.markdown("**Adapter content**")
        if plots["adapters"]:
            frame = pd.DataFrame(plots["adapters"])
            st.line_chart(frame, x="Position", y=[c for c in frame if c not in {"Position", "Base group"}],
                          x_label="Base position (group midpoint)", y_label="Reads with adapter (%)")
        else:
            st.caption("This module produced no plot data.")
    try:
        columns = st.columns(3)
        columns[0].download_button("Download FastQC HTML", fastqc.read_artifact(directory, analysis, item["html"]),
                                   file_name=Path(choice).name.removesuffix(".fastq.gz") + "_fastqc.html", mime="text/html", key="bqc_fastqc_html")
        columns[1].download_button("Download FastQC ZIP", fastqc.read_artifact(directory, analysis, item["zip"]),
                                   file_name=Path(choice).name.removesuffix(".fastq.gz") + "_fastqc.zip", mime="application/zip", key="bqc_fastqc_zip")
        columns[2].download_button("Download FastQC analysis JSON", json.dumps(analysis, indent=2, ensure_ascii=False, allow_nan=False),
                                   file_name="fastqc_analysis.json", mime="application/json", key="bqc_fastqc_json")
        with st.expander("Analysis settings, fingerprints and execution log"):
            st.code(str(directory), language=None)
            st.json({"tool": analysis["tool"], "command": analysis["command"], "input_sha256": analysis["input_sha256"],
                     "validation_sha256": analysis["validation_sha256"]})
            log = fastqc.read_artifact(directory, analysis, "fastqc.log").decode("utf-8", errors="replace")
            st.code(log[-10000:], language=None)
    except Exception as error:
        st.error(f"FastQC download is unavailable: {error}")
