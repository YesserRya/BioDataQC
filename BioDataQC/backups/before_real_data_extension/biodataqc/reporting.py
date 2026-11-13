"""Self-contained HTML from one saved validation report. No network assets or JS."""

from __future__ import annotations

import json
import os
import tempfile
from html import escape
from pathlib import Path

from .report_io import check_report_destination


def _text(value):
    return escape(str(value if value is not None else "—"), quote=True)


def _table(headers, rows):
    head = "".join(f"<th scope='col'>{_text(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _badge(value):
    known = {"READY", "BLOCKED", "INCOMPLETE", "PASS", "FAIL", "ERROR", "NOT_RUN", "WARNING", "BLOCKER"}
    color = str(value).lower() if value in known else "unknown"
    return f"<span class='badge {color}'>{_text(value)}</span>"


def render_report(evidence: dict, *, owner: str | None = None, next_update: str | None = None) -> str:
    status = evidence["status"]
    if status not in {"READY", "BLOCKED", "INCOMPLETE"}:
        raise ValueError(f"Unsupported report status: {status}")
    counts, config = evidence["counts"], evidence["requirements"]
    summary = evidence.get("inventory_summary", {})
    findings = [c for c in evidence["checks"] if c["outcome"] in {"FAIL", "ERROR"}]
    blockers = counts["blocking_findings"]
    messages = {
        "READY": ("Eligible for verified local staging", "Required validation checks completed without blocking findings. Packaging still needs to confirm unchanged inputs and verify the staged copy."),
        "BLOCKED": ("Delivery preparation is blocked", f"This run identified {blockers} blocking finding(s). Resolve the reported input problems and create a new validation run before preparing a delivery."),
        "INCOMPLETE": ("A readiness decision is incomplete", "An execution problem or unfinished required check prevents readiness. Complete the missing checks in a new validation run before preparing a delivery."),
    }
    headline, impact = messages[status]
    affected = sorted({c["sample_id"] for c in findings if c.get("sample_id")})
    scope = ", ".join(affected) if affected else "None named in findings"
    if any(not c.get("sample_id") for c in findings):
        scope += "; dataset-level findings also require review"
    finding_rows = []
    for c in findings:
        where = c.get("file_path") or c.get("sample_id") or "Dataset"
        if c.get("record_number") is not None:
            where += f" · record {c['record_number']}"
        finding_rows.append([
            _badge(c["severity"]) + f"<small>{_text(c['rule_id'])} · {_text(c['outcome'])}</small>",
            f"<strong>{_text(where)}</strong><p>{_text(c['message'])}</p>",
            _text(c.get("next_action") or "Review the check evidence."),
        ])
    findings_html = (_table(["Finding", "Location & explanation", "Next action"], finding_rows)
                     if finding_rows else "<div class='empty-state'>No blocking findings, warnings or execution errors were recorded.</div>")
    checks = []
    for c in evidence["checks"]:
        location = c.get("file_path") or c.get("sample_id") or "Dataset"
        dependency = ("<small>Depends on: " + _text(", ".join(c["blocked_by"])) + "</small>") if c["blocked_by"] else ""
        record = f"<small>Record {_text(c['record_number'])}</small>" if c.get("record_number") is not None else ""
        checks.append([_text(c["rule_id"]), _badge(c["outcome"]), "Required" if c["required"] else "Optional",
                       _text(location) + record, _text(c["message"]) + dependency])
    inventory = [[_text(e["relative_path"]), _text(e.get("sample_id")), _text(e.get("read")),
                  _text(e["status"]), _text(e.get("size_bytes"))] for e in evidence["inventory"]]
    metadata = [[_text(row.get(column, "")) for column in evidence.get("metadata_columns", [])]
                for row in evidence.get("metadata_rows", [])]
    fingerprints = [[_text(path), f"<code>{_text(digest)}</code>"]
                    for path, digest in sorted(evidence["input_sha256"].items())]
    stat_cards = [
        ("Expected samples", len(config.get("expected_samples", []))),
        ("Expected files present", f"{summary.get('present_expected_files', 0)} / {summary.get('expected_files', 0)}"),
        ("Blocking findings", blockers), ("Warnings", counts["warnings"]),
    ]
    cards = "".join(f"<div class='metric'><span>{_text(label)}</span><strong>{_text(value)}</strong></div>" for label, value in stat_cards)
    technical = _table(["Measure", "Recorded result"], [[_text(k.replace("_", " ").capitalize()), _text(v)] for k, v in counts.items()])
    project = evidence.get("project_id") or "Project unavailable"
    client = config.get("client_label") or "Client not specified"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>BioDataQC · {_text(project)} · {_text(status)}</title>
<style>
:root{{--ink:#172d40;--muted:#596d7c;--line:#dbe4e9;--paper:#fff;--bg:#f1f5f7;--blue:#164d73}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
a{{color:var(--blue)}}.topbar{{background:#12344b;color:#fff}}.topbar>div{{max-width:1180px;margin:auto;padding:20px 32px;display:flex;justify-content:space-between;gap:16px}}
.brand{{font-size:20px;font-weight:750;letter-spacing:-.5px}}.eyebrow{{text-transform:uppercase;font-size:11px;letter-spacing:1.6px;font-weight:700}}
main{{max-width:1180px;margin:auto;padding:32px}}.hero{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;margin-bottom:24px}}
h1{{font-size:34px;line-height:1.2;letter-spacing:-1px;margin:8px 0 10px}}h2{{font-size:21px;letter-spacing:-.4px;margin:0 0 15px}}p{{margin:8px 0}}small{{display:block;color:var(--muted);font-size:12px;margin-top:7px}}
.muted{{color:var(--muted)}}.badge{{display:inline-block;padding:5px 10px;border-radius:5px;font-size:11px;font-weight:800;letter-spacing:.5px;background:#e9eef2;color:#344a5c;white-space:nowrap}}
.ready,.pass{{background:#dcf3e7;color:#165d3b}}.blocked,.fail,.blocker{{background:#fbe3e0;color:#962f27}}.incomplete,.error,.warning{{background:#fff0d3;color:#805611}}.not_run{{background:#e8edf3;color:#4c6074}}
.hero>.badge{{font-size:13px;padding:9px 15px;margin-top:10px}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:24px 0}}
.metric{{background:var(--paper);border:1px solid var(--line);border-radius:9px;padding:18px 20px}}.metric span{{color:var(--muted);font-size:12px;display:block}}.metric strong{{font-size:30px;font-weight:720;display:block;margin-top:4px}}
section{{background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:24px;margin:20px 0}}.decision{{border-left:5px solid #1b6b95}}.decision.blocked-panel{{border-left-color:#bb5147}}.decision.ready-panel{{border-left-color:#2e8060}}
.decision h2{{margin:7px 0 10px}}.contact-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:20px;border-top:1px solid var(--line);padding-top:16px;margin-top:20px}}.contact-grid strong{{display:block;font-size:13px;margin-top:4px;overflow-wrap:anywhere}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th{{background:#f3f6f8;color:#415b6c;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:12px}}td{{padding:13px 12px;vertical-align:top;border-bottom:1px solid #e5ebef;overflow-wrap:anywhere}}td p{{margin:6px 0 0}}tr:last-child td{{border-bottom:0}}
code,pre{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}}code{{overflow-wrap:anywhere}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;padding:16px;background:#f3f6f8;border-radius:5px}}
details{{margin-top:18px;border-top:1px solid var(--line);padding-top:15px}}summary{{cursor:pointer;font-weight:650;margin-bottom:12px}}.empty-state{{padding:18px;background:#edf7f1;border:1px solid #d4e7dc;border-radius:6px;color:#245b40}}.note{{padding:13px 16px;background:#edf2f6;border-radius:5px;font-size:12px;color:#41576a;margin-top:16px}}
footer{{font-size:12px;color:var(--muted);padding:6px 0 26px}}.trace{{overflow-wrap:anywhere}}nav{{font-size:12px;margin-top:14px}}nav a{{margin-right:18px;text-decoration:none;font-weight:600}}
@media(max-width:700px){{main{{padding:20px 16px}}.topbar>div{{padding:16px}}.topbar .eyebrow{{display:none}}h1{{font-size:27px}}.hero{{gap:12px}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.metric{{padding:14px}}section{{padding:18px}}.contact-grid{{grid-template-columns:1fr;gap:12px}}}}
@media print{{body{{background:#fff;font-size:11px}}main{{padding:0}}.topbar{{background:#fff;color:#172d40}}.topbar>div{{padding:12px 0}}section,.metric{{break-inside:avoid}}nav{{display:none}}.table-wrap{{overflow:visible}}}}
</style></head><body>
<header class="topbar"><div><span class="brand">BioDataQC</span><span class="eyebrow">Delivery quality · Synthetic demo</span></div></header>
<main><div class="hero"><div><div class="eyebrow muted">{_text(project)} / {_text(client)}</div><h1>Delivery readiness report</h1>
<p class="muted">Saved validation evidence · Finished {_text(evidence.get('finished_at'))}</p>
<nav aria-label="Report sections"><a href="#decision">Delivery summary</a><a href="#findings">Findings</a><a href="#technical">Technical evidence</a></nav></div>{_badge(status)}</div>
<div class="metrics">{cards}</div>
<section id="decision" class="decision {status.lower()}-panel"><div class="eyebrow muted">Delivery summary</div><h2>{_text(headline)}</h2><p>{_text(impact)}</p>
<div class="contact-grid"><div><span class="muted">Responsible person / team</span><strong>{_text(owner or 'Not assigned')}</strong></div><div><span class="muted">Next update</span><strong>{_text(next_update or 'To be confirmed')}</strong></div><div><span class="muted">Samples named in findings</span><strong>{_text(scope)}</strong></div></div>
<div class="note">This status describes the saved run. Current source files have not been revalidated by generating this report. Owner and update information, when supplied, is entered by the operator.</div></section>
<section id="findings"><h2>Findings & next actions</h2>{findings_html}</section>
<section id="technical"><h2>Technical evidence</h2><p class="trace"><strong>Run ID</strong> <code>{_text(evidence['run_id'])}</code><br><strong>Rules</strong> {_text(evidence.get('rules_version'))} · <strong>Requirements</strong> {_text(config.get('specification_version'))}</p>
{technical}
<details><summary>All {len(evidence['checks'])} check outcomes</summary>{_table(['Rule', 'Outcome', 'Scope', 'Location', 'Evidence'], checks)}</details>
<details><summary>File inventory · {len(inventory)} entries</summary>{_table(['Relative path', 'Sample', 'Read', 'Inventory status', 'Bytes'], inventory)}</details>
<details><summary>Sample metadata · {len(metadata)} records</summary>{_table(evidence.get('metadata_columns', []), metadata) if metadata else '<p>No metadata rows were available.</p>'}</details>
<details><summary>Input fingerprints · {len(fingerprints)} files</summary>{_table(['Relative path', 'SHA-256'], fingerprints)}</details>
<details><summary>Applied requirements</summary><pre>{_text(json.dumps(config, indent=2, ensure_ascii=False))}</pre></details></section>
<footer>Local synthetic-data prototype. Structural validation and copy consistency are assessed; biological quality and pre-receipt integrity are outside this profile.</footer></main></body></html>
"""


def write_report_html(evidence: dict, destination: str | Path, *, owner=None, next_update=None,
                      protected_paths=()) -> Path:
    destination = check_report_destination(evidence, destination, protected_paths)
    html = render_report(evidence, owner=owner, next_update=next_update)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=".report-", suffix=".html", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(html)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination
