"""`export_access_request_report` -- a one-page sheet of one person's access requests.

The access-request scenario ends with a deliverable: a table the employee (or their
manager) can open and read without knowing anything about ISC. For each request it
says what the access is, where it stands, and who needs to approve it or who
already did. Pending items come first.

The two-sentence note per row is built from the request's own data rather than
written by a model, so the sheet is the same every time it is generated and can be
trusted as evidence. The assistant is free to add colour in chat on top of it.
"""

from __future__ import annotations

import csv
import html
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from sailpoint import AccessRequestApprovalsApi, AccessRequestsApi

from ..client import call_sailpoint, describe_api_error
from .access_requests import DEFAULT_DAYS, MAX_DAYS, MAX_LIMIT, _since, summarize_request

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "output"

# Section order is the reading order: what needs attention first.
SECTIONS = [
    ("pending_approval", "Pending approval"),
    ("in_progress", "In progress"),
    ("completed", "Recently approved"),
    ("not_granted", "Not granted"),
]
_NOT_GRANTED = {"rejected", "failed", "cancelled"}

COLUMNS = [
    ("section", "Section"),
    ("item", "Access"),
    ("item_type", "Type"),
    ("status_text", "Status"),
    ("approver", "Approver"),
    ("summary", "What it is / where it stands"),
    ("requested_by", "Requested by"),
    ("requested_on", "Requested on"),
    ("days_open", "Days open"),
    ("request_id", "Request ID"),
]

_PHASE_TEXT = {
    "SOD_PHASE": "Being checked for separation-of-duties conflicts.",
    "APPROVAL_PHASE": "Waiting on an approver.",
    "PROVISIONING_PHASE": "Approved and being set up.",
}


def _names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _sentence(text: str | None) -> str:
    text = " ".join((text or "").split())
    if not text:
        return "No description was provided for this access."
    return text if text[-1] in ".!?" else text + "."


def _section(status: str | None) -> str:
    return "not_granted" if status in _NOT_GRANTED else (
        status if status in {"pending_approval", "in_progress", "completed"} else "in_progress"
    )


def _standing(request: dict[str, Any]) -> tuple[str, str]:
    """(short status, sentence about who is involved) for one request."""
    status = request.get("status")
    if status == "pending_approval":
        waiting = _names(request.get("waiting_on") or [])
        return "Pending approval", (
            f"Waiting on {waiting} to approve." if waiting else "Waiting on an approver."
        )
    if status == "in_progress":
        approvers = _names(request.get("approved_by") or [])
        text = _PHASE_TEXT.get(request.get("current_phase") or "", "Being processed.")
        return "In progress", (f"Approved by {approvers}. {text}" if approvers else text)
    if status == "completed":
        approvers = _names(request.get("approved_by") or [])
        return "Approved", (
            f"Approved by {approvers} and granted."
            if approvers
            else "Granted automatically; no approval was required."
        )
    if status == "rejected":
        rejecters = _names(request.get("rejected_by") or [])
        return "Rejected", (f"Rejected by {rejecters}." if rejecters else "Rejected.")
    if status == "failed":
        reason = (request.get("errors") or [""])[0]
        return "Failed", "Approved but could not be set up." + (f" {reason}" if reason else "")
    if status == "cancelled":
        return "Cancelled", "This request was cancelled."
    return "Unknown", "Status could not be determined."


def pending_approvers(request_ids: list[str]) -> dict[str, list[str]]:
    """Who each pending request is waiting on, straight from the approvals API.

    The request-status record only names an approver for single-person schemes.
    For "all owners" or group approval it has no name at all, while the approval
    itself always carries the real owner -- so ask it. Needs an org-admin PAT to
    see other people's approvals; on failure the caller falls back to the
    status record.
    """
    if not request_ids:
        return {}
    quoted = ",".join(f'"{i}"' for i in request_ids)
    approvals = call_sailpoint(
        lambda client: AccessRequestApprovalsApi(client).list_pending_approvals_v1(
            filters=f"accessRequestId in ({quoted})", limit=MAX_LIMIT
        )
    )
    found: dict[str, list[str]] = {}
    for approval in approvals or []:
        owner = approval.owner.name if approval.owner else None
        if approval.access_request_id and owner:
            found.setdefault(approval.access_request_id, []).append(owner)
    return {key: sorted(set(names)) for key, names in found.items()}


def build_report_rows(
    requests: list[dict[str, Any]], now: datetime | None = None
) -> list[dict[str, Any]]:
    """One display row per request, grouped by section in reading order."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []

    for request in requests:
        status_text, who = _standing(request)
        created = request.get("created")
        days_open = None
        if created:
            try:
                days_open = (now - datetime.fromisoformat(created)).days
            except ValueError:
                pass

        approvers = (
            request.get("waiting_on")
            if request.get("status") == "pending_approval"
            else request.get("approved_by") or request.get("rejected_by")
        )
        rows.append(
            {
                "section": _section(request.get("status")),
                "item": request.get("item"),
                "item_type": (request.get("item_type") or "").replace("_", " ").title(),
                "status_text": status_text,
                "approver": _names(approvers or []) or None,
                "summary": f"{_sentence(request.get('description'))} {who}",
                "requested_by": request.get("requested_by"),
                "requested_on": (created or "")[:10],
                "days_open": days_open,
                "request_id": request.get("request_id"),
            }
        )

    order = {key: index for index, (key, _) in enumerate(SECTIONS)}
    # Within a section, newest first (ISO dates sort as text).
    rows.sort(key=lambda r: r["requested_on"], reverse=True)
    rows.sort(key=lambda r: order[r["section"]])
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    titles = dict(SECTIONS)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(title for _, title in COLUMNS)
        for row in rows:
            writer.writerow(
                titles[row["section"]] if key == "section" else row.get(key)
                for key, _ in COLUMNS
            )


def render_html(person: str, days: int, rows: list[dict[str, Any]]) -> str:
    """A self-contained page: open it, read it, print it."""
    titles = dict(SECTIONS)
    esc = lambda value: html.escape("" if value is None else str(value))  # noqa: E731
    shown = [(key, title) for key, title in COLUMNS if key not in ("section", "request_id")]

    body = []
    for key, title in SECTIONS:
        section_rows = [r for r in rows if r["section"] == key]
        if not section_rows:
            continue
        body.append(f"<h2>{esc(title)} <span>({len(section_rows)})</span></h2>")
        body.append("<table><tr>" + "".join(f"<th>{esc(t)}</th>" for _, t in shown) + "</tr>")
        for row in section_rows:
            cells = "".join(f"<td>{esc(row.get(k))}</td>" for k, _ in shown)
            body.append(f'<tr class="{key}">{cells}</tr>')
        body.append("</table>")
    if not rows:
        body.append(f"<p>No access requests in the last {days} days.</p>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Access requests for {esc(person)}</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:2rem;color:#1b2430}}
 h1{{margin-bottom:.2rem}} .sub{{color:#5b6573;margin-top:0}}
 h2{{margin-top:2rem}} h2 span{{color:#5b6573;font-weight:400}}
 table{{border-collapse:collapse;width:100%;font-size:.92rem}}
 th,td{{border:1px solid #d5dbe3;padding:.5rem .6rem;text-align:left;vertical-align:top}}
 th{{background:#f1f4f8}} tr.pending_approval td:nth-child(3){{color:#b45309;font-weight:600}}
 tr.completed td:nth-child(3){{color:#15803d;font-weight:600}}
 tr.not_granted td:nth-child(3){{color:#b91c1c;font-weight:600}}
 @media print{{body{{margin:.5in}}}}
</style></head><body>
<h1>Access requests for {esc(person)}</h1>
<p class="sub">Last {days} days &middot; generated {datetime.now():%Y-%m-%d %H:%M}</p>
{"".join(body)}
</body></html>"""


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="export_access_request_report")
    def export_access_request_report(
        identity_id: str, days: int = DEFAULT_DAYS, output_dir: str | None = None
    ) -> dict[str, Any]:
        """Create a shareable sheet of one person's access requests: what is still
        pending, what was recently approved, and who approves or approved each one.

        Use when the user wants a report, sheet, table or something to send or
        keep -- "give me a report of Adam's access requests", "what's pending and
        what got approved for me, as a sheet". For a quick chat answer without a
        file, use `get_access_request_status` instead. Find the person's `id` with
        `search_identities` first and confirm which person if several match.

        Args:
            identity_id: The `id` of the person the requests are for.
            days: Look back this many days. Default 30, max 365.
            output_dir: Folder for the files. Defaults to the project's `output/`.

        Returns:
            `csv_path` and `html_path` (open the HTML to read or print it; the CSV
            opens in Excel), counts per section, and `rows`. Each row has a plain
            `summary` of what the access is and who needs to approve it or who
            approved it. Show the user the pending rows first, quote the summaries
            as written, and give them the file paths.
        """
        days = max(1, min(days, MAX_DAYS))
        try:
            items = call_sailpoint(
                lambda client: AccessRequestsApi(client).list_access_request_status_v1(
                    requested_for=identity_id,
                    limit=MAX_LIMIT,
                    filters=f"created ge {_since(days)}",
                    sorters="-created",
                )
            )
        except Exception as exc:
            log.exception("export_access_request_report failed")
            return {"error": describe_api_error(exc)}

        items = items or []
        requests = [summarize_request(item.to_dict()) for item in items]

        pending = [r for r in requests if r.get("status") == "pending_approval"]
        try:
            approvers = pending_approvers([r["request_id"] for r in pending if r.get("request_id")])
        except Exception as exc:  # the report is still useful without names
            log.warning("could not look up pending approvers: %s", exc)
            approvers = {}
        for request in pending:
            names = approvers.get(request.get("request_id"))
            if names:
                request["waiting_on"] = sorted(set(request.get("waiting_on", [])) | set(names))

        person = next((r["requested_for"] for r in requests if r.get("requested_for")), identity_id)
        rows = build_report_rows(requests)

        folder = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        slug = re.sub(r"[^A-Za-z0-9]+", "_", person).strip("_") or "identity"
        stem = f"access_requests_{slug}_{datetime.now():%Y%m%d_%H%M%S}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_csv(rows, folder / f"{stem}.csv")
            (folder / f"{stem}.html").write_text(
                render_html(person, days, rows), encoding="utf-8"
            )
        except OSError as exc:
            return {"error": f"Could not write the report: {exc}"}

        response: dict[str, Any] = {
            "person": person,
            "days": days,
            "csv_path": str(folder / f"{stem}.csv"),
            "html_path": str(folder / f"{stem}.html"),
            "counts": {
                title: sum(1 for r in rows if r["section"] == key) for key, title in SECTIONS
            },
            "rows": [
                {k: v for k, v in r.items() if k in {"section", "item", "status_text", "approver", "summary", "requested_by", "requested_on", "days_open"} and v not in (None, "")}
                for r in rows
            ],
        }
        if len(items) == MAX_LIMIT:
            response["note"] = f"Hit the limit of {MAX_LIMIT}; older requests are not included."
        return response
