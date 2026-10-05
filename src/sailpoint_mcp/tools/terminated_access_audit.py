"""`audit_terminated_access` -- which people who left still have live access?

A quick check for a security manager or auditor: pull the identities in a
terminated/inactive lifecycle state, look at every account correlated to them, and
report the ones that are still enabled -- with the source, how many groups or
entitlements the account holds, and whether anything is privileged. The result is a
CSV (one row per live account, for follow-up) and a printable HTML summary.

ISC can search for these identities, but turning "terminated" plus "accounts that
are still enabled" into a single reviewable list takes custom searching and manual
cross-referencing; this does it in one question.
"""

from __future__ import annotations

import csv
import html
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from sailpoint import SearchApi
from sailpoint.search.models.query import Query
from sailpoint.search.models.query_result_filter import QueryResultFilter
from sailpoint.search.models.search import Search

from ..client import call_sailpoint, describe_api_error

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "output"

PAGE_SIZE = 250
MAX_IDENTITIES = 1000

FIELDS = [
    "id",
    "name",
    "displayName",
    "email",
    "attributes.jobTitle",
    "attributes.department",
    "attributes.cloudLifecycleState",
    "manager.displayName",
    "accessCount",
    "accounts",
    "access",
]

CSV_COLUMNS = [
    ("risk", "Risk"),
    ("identity", "Identity"),
    ("job_title", "Job title"),
    ("department", "Department"),
    ("manager", "Manager"),
    ("lifecycle_state", "Lifecycle state"),
    ("source", "Source"),
    ("account", "Account"),
    ("entitlements", "Groups / entitlements on account"),
    ("privileged_account", "Privileged account"),
    ("locked", "Locked"),
    ("account_created", "Account created"),
    ("why", "Why it matters"),
]


def build_query(lifecycle_state: str) -> str:
    """Identities in the given lifecycle state, or flagged inactive outright."""
    state = lifecycle_state.replace('"', "").strip() or "inactive"
    return f'(attributes.cloudLifecycleState:"{state}" OR inactive:true)'


def _entitlement_count(account: dict[str, Any]) -> int:
    """Groups/entitlements held on an account, whatever the connector calls them."""
    total = 0
    for value in (account.get("entitlementAttributes") or {}).values():
        total += len(value) if isinstance(value, list) else (1 if value else 0)
    return total


def assess(document: dict[str, Any]) -> dict[str, Any] | None:
    """Findings for one terminated identity, or None if nothing is still enabled."""
    attributes = document.get("attributes") or {}
    live = [
        a for a in document.get("accounts") or []
        if isinstance(a, dict) and not a.get("disabled")
    ]
    if not live:
        return None

    privileged_access = sorted(
        {
            item.get("displayName") or item.get("name")
            for item in document.get("access") or []
            if isinstance(item, dict) and item.get("privileged")
        }
        - {None}
    )
    accounts = [
        {
            "source": (a.get("source") or {}).get("name"),
            "account": a.get("name"),
            "entitlements": _entitlement_count(a),
            "privileged": bool(a.get("privileged")),
            "locked": bool(a.get("locked")),
            "created": (a.get("created") or "")[:10],
        }
        for a in live
    ]

    if privileged_access or any(a["privileged"] for a in accounts):
        risk = "High"
    elif any(a["entitlements"] for a in accounts):
        risk = "Medium"
    else:
        risk = "Low"

    return {
        "id": document.get("id"),
        "identity": document.get("displayName") or document.get("name"),
        "job_title": attributes.get("jobTitle"),
        "department": attributes.get("department"),
        "manager": (document.get("manager") or {}).get("displayName"),
        "lifecycle_state": attributes.get("cloudLifecycleState"),
        "risk": risk,
        "live_accounts": accounts,
        "privileged_access": privileged_access,
        "total_access": document.get("accessCount"),
    }


def _why(finding: dict[str, Any], account: dict[str, Any]) -> str:
    parts = [f"{finding['identity']} is {finding['lifecycle_state'] or 'inactive'} but this account is still enabled"]
    if account["entitlements"]:
        parts.append(f"with {account['entitlements']} group(s)/entitlement(s)")
    if account["privileged"]:
        parts.append("and is privileged")
    return " ".join(parts) + "."


def flatten(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One CSV row per live account, highest risk first."""
    rank = {"High": 0, "Medium": 1, "Low": 2}
    rows = []
    for finding in findings:
        for account in finding["live_accounts"]:
            rows.append(
                {
                    "risk": finding["risk"],
                    "identity": finding["identity"],
                    "job_title": finding["job_title"],
                    "department": finding["department"],
                    "manager": finding["manager"],
                    "lifecycle_state": finding["lifecycle_state"],
                    "source": account["source"],
                    "account": account["account"],
                    "entitlements": account["entitlements"],
                    "privileged_account": "Yes" if account["privileged"] else "No",
                    "locked": "Yes" if account["locked"] else "No",
                    "account_created": account["created"],
                    "why": _why(finding, account),
                }
            )
    rows.sort(key=lambda r: (rank[r["risk"]], r["identity"] or "", r["source"] or ""))
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(title for _, title in CSV_COLUMNS)
        for row in rows:
            writer.writerow(row.get(key) for key, _ in CSV_COLUMNS)


def render_html(total: int, findings: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    esc = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
    high = sum(1 for f in findings if f["risk"] == "High")
    shown = [(k, t) for k, t in CSV_COLUMNS if k not in ("lifecycle_state", "why", "account_created")]
    head = "".join(f"<th>{esc(t)}</th>" for _, t in shown)
    body = "".join(
        f'<tr class="{r["risk"].lower()}">' + "".join(f"<td>{esc(r.get(k))}</td>" for k, _ in shown) + "</tr>"
        for r in rows
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Terminated users with live access</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:2rem;color:#1b2430}}
 .stats{{display:flex;gap:1rem;margin:1rem 0 2rem}}
 .stat{{border:1px solid #d5dbe3;border-radius:8px;padding:.8rem 1.2rem}}
 .stat b{{display:block;font-size:1.8rem}}
 table{{border-collapse:collapse;width:100%;font-size:.9rem}}
 th,td{{border:1px solid #d5dbe3;padding:.45rem .6rem;text-align:left}} th{{background:#f1f4f8}}
 tr.high td:first-child{{color:#b91c1c;font-weight:700}} tr.medium td:first-child{{color:#b45309;font-weight:700}}
</style></head><body>
<h1>Terminated users who still have live access</h1>
<p>Generated {datetime.now():%Y-%m-%d %H:%M}</p>
<div class="stats">
 <div class="stat"><b>{total}</b>terminated identities</div>
 <div class="stat"><b>{len(findings)}</b>still have an enabled account</div>
 <div class="stat"><b>{len(rows)}</b>enabled accounts</div>
 <div class="stat"><b>{high}</b>high risk (privileged)</div>
</div>
<table><tr>{head}</tr>{body}</table>
</body></html>"""


def fetch_identities(lifecycle_state: str) -> tuple[list[dict[str, Any]], bool]:
    """All matching identities, paged. Second value is True if the cap was hit."""
    search = Search(
        indices=["identities"],
        query=Query(query=build_query(lifecycle_state)),
        query_result_filter=QueryResultFilter(includes=FIELDS),
        sort=["id"],
        include_nested=True,
    )
    found: list[dict[str, Any]] = []
    while len(found) < MAX_IDENTITIES:
        page = call_sailpoint(
            lambda client: SearchApi(client).search_post_v1(
                search=search, limit=PAGE_SIZE, offset=len(found)
            )
        )
        found.extend(d for d in page or [] if isinstance(d, dict))
        if not page or len(page) < PAGE_SIZE:
            return found, False
    return found, True


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="audit_terminated_access")
    def audit_terminated_access(
        lifecycle_state: str = "inactive", output_dir: str | None = None
    ) -> dict[str, Any]:
        """Find terminated people who still have enabled accounts, and export a report.

        Use for a security or audit check: "how many terminated users still have
        active access?", "which leavers still have accounts?", "who left but can
        still log in?". It pulls identities in the given lifecycle state, looks at
        every account correlated to them, and keeps the ones that are still
        enabled. Risk is High if a privileged account or privileged access
        remains, Medium if an enabled account holds groups/entitlements, else Low.

        Args:
            lifecycle_state: The lifecycle state that means "terminated" in this
                tenant. Default `inactive`; some tenants use `terminated` or
                `leaver`.
            output_dir: Folder for the files. Defaults to the project's `output/`.

        Returns:
            Counts, a per-identity list (highest risk first) with their live
            accounts, and `csv_path` / `html_path` for the report. Lead your
            answer with the headline numbers, then the highest-risk people.
        """
        try:
            documents, capped = fetch_identities(lifecycle_state)
        except Exception as exc:
            log.exception("audit_terminated_access failed")
            return {"error": describe_api_error(exc)}

        findings = [f for f in (assess(d) for d in documents) if f]
        rank = {"High": 0, "Medium": 1, "Low": 2}
        findings.sort(key=lambda f: (rank[f["risk"]], f["identity"] or ""))
        rows = flatten(findings)

        folder = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        stem = f"terminated_access_{datetime.now():%Y%m%d_%H%M%S}"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            write_csv(rows, folder / f"{stem}.csv")
            (folder / f"{stem}.html").write_text(
                render_html(len(documents), findings, rows), encoding="utf-8"
            )
        except OSError as exc:
            return {"error": f"Could not write the report: {exc}"}

        response: dict[str, Any] = {
            "lifecycle_state": lifecycle_state,
            "terminated_identities": len(documents),
            "with_enabled_accounts": len(findings),
            "enabled_accounts": len(rows),
            "by_risk": {r: sum(1 for f in findings if f["risk"] == r) for r in rank},
            "csv_path": str(folder / f"{stem}.csv"),
            "html_path": str(folder / f"{stem}.html"),
            "identities": [
                {
                    "identity": f["identity"],
                    "job_title": f["job_title"],
                    "risk": f["risk"],
                    "enabled_accounts": [
                        f"{a['source']} ({a['entitlements']} groups)" for a in f["live_accounts"]
                    ],
                    "privileged_access": f["privileged_access"],
                }
                for f in findings
            ],
        }
        if capped:
            response["note"] = f"Stopped at {MAX_IDENTITIES} identities; the list is incomplete."
        if not documents:
            response["note"] = f"No identities found in lifecycle state '{lifecycle_state}'."
        return response
