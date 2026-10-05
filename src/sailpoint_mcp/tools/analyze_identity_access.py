"""`analyze_identity_access` -- everything one person can access, as a CSV plus a digest.

Built for the leadership question "what access does <person> have?". The flow:

  1. `search_identities` finds candidates and shows job titles; the user picks one.
  2. This tool takes that identity's id and pulls the full picture in a handful of
     calls: the identity's nested access (roles, access profiles, entitlements)
     and accounts from the Search API, then enriches it in parallel -- which
     entitlements each access profile / role bundles, and entitlement owners and
     privilege levels.
  3. Every row goes into a CSV (the raw evidence). The tool returns a compact
     digest -- counts, per-source breakdown, privileged items -- which the model
     turns into the plain-English summary.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from sailpoint import AccessProfilesApi, EntitlementsApi, RolesApi, SearchApi
from sailpoint.search.models.query import Query
from sailpoint.search.models.query_result_filter import QueryResultFilter
from sailpoint.search.models.search import Search

from ..client import call_sailpoint, describe_api_error

log = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "output"

# Enrichment is one call per access profile / role. Cap it so a heavily
# provisioned executive cannot turn one question into hundreds of requests.
MAX_EXPANSIONS = 40
EXPANSION_WORKERS = 8
ENTITLEMENT_BATCH = 50
ENTITLEMENT_PAGE = 250

IDENTITY_FIELDS = [
    "id",
    "name",
    "displayName",
    "email",
    "lifecycleState",
    "inactive",
    "manager.displayName",
    "attributes.department",
    "attributes.jobTitle",
    "attributes.cloudLifecycleState",
    "access",
    "accounts",
]

CSV_COLUMNS = [
    "identity",
    "access_type",
    "name",
    "source",
    "description",
    "attribute",
    "value",
    "privileged",
    "owner",
    "requestable",
    "assignment",
    "granted_via",
    "id",
]


def _name(value: Any) -> str | None:
    """Pull a display string out of a ref-like dict (`{"name": ...}`)."""
    if isinstance(value, dict):
        return value.get("name") or value.get("displayName")
    return value if isinstance(value, str) else None


def _as_dict(model: Any) -> dict[str, Any]:
    return model.to_dict() if hasattr(model, "to_dict") else dict(model or {})


def fetch_identity_document(identity_id: str) -> dict[str, Any] | None:
    """One Search API call returns the identity plus its nested access and accounts."""
    search = Search(
        indices=["identities"],
        query=Query(query=f'id:"{identity_id}"'),
        query_result_filter=QueryResultFilter(includes=IDENTITY_FIELDS),
        include_nested=True,
    )
    results = call_sailpoint(
        lambda client: SearchApi(client).search_post_v1(search=search, limit=1)
    )
    return results[0] if results else None


def _expand_access_profile(profile_id: str) -> list[dict[str, Any]]:
    entitlements = call_sailpoint(
        lambda client: AccessProfilesApi(client).get_access_profile_entitlements_v1(
            id=profile_id, limit=ENTITLEMENT_PAGE
        )
    )
    return [_as_dict(e) for e in entitlements or []]


def _expand_role(role_id: str) -> dict[str, Any]:
    # The SDK's Role model deserializes to just `name` (its fields are lost in a
    # oneOf wrapper), so read the raw JSON to get entitlements / accessProfiles.
    response = call_sailpoint(
        lambda client: RolesApi(client).get_role_v1_without_preload_content(id=role_id)
    )
    return json.loads(response.data)


def _fetch_entitlement_details(ids: list[str]) -> dict[str, dict[str, Any]]:
    """Owner / privilege / requestable for a set of entitlement ids, in batches."""
    details: dict[str, dict[str, Any]] = {}
    batches = [ids[i : i + ENTITLEMENT_BATCH] for i in range(0, len(ids), ENTITLEMENT_BATCH)]

    def fetch(batch: list[str]) -> list[dict[str, Any]]:
        quoted = ",".join(f'"{i}"' for i in batch)
        found = call_sailpoint(
            lambda client: EntitlementsApi(client).list_entitlements_v1(
                filters=f"id in ({quoted})", limit=ENTITLEMENT_PAGE
            )
        )
        return [_as_dict(e) for e in found or []]

    with ThreadPoolExecutor(EXPANSION_WORKERS) as pool:
        for batch_result in pool.map(fetch, batches):
            for entitlement in batch_result:
                if entitlement.get("id"):
                    details[entitlement["id"]] = entitlement
    return details


def build_rows(
    document: dict[str, Any],
    bundles: dict[str, list[str]],
    details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Turn the identity document into one flat row per access item and account.

    `bundles` maps an entitlement id to the names of the access profiles / roles
    known to grant it; `details` carries owner and privilege data per entitlement.
    """
    identity = document.get("displayName") or document.get("name")
    rows: list[dict[str, Any]] = []

    for item in document.get("access") or []:
        if not isinstance(item, dict):
            continue
        access_type = item.get("type")
        detail = details.get(item.get("id"), {}) if access_type == "ENTITLEMENT" else {}
        privilege = (detail.get("privilege_level") or detail.get("privilegeLevel") or {})
        privileged = item.get("privileged")
        if privilege.get("direct") is True or privilege.get("effective") == "HIGH":
            privileged = True

        granted_via = bundles.get(item.get("id"), [])
        rows.append(
            {
                "identity": identity,
                "access_type": access_type,
                "name": item.get("displayName") or item.get("name"),
                "source": _name(item.get("source")),
                "description": item.get("description"),
                "attribute": item.get("attribute"),
                "value": item.get("value"),
                "privileged": privileged,
                "owner": _name(item.get("owner")) or _name(detail.get("owner")),
                "requestable": detail.get("requestable"),
                # Derived from the expansion above: an entitlement no profile or
                # role bundles was assigned on its own.
                "assignment": (
                    ("via bundle" if granted_via else "direct")
                    if access_type == "ENTITLEMENT"
                    else None
                ),
                "granted_via": "; ".join(sorted(set(granted_via))) or None,
                "id": item.get("id"),
            }
        )

    for account in document.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        rows.append(
            {
                "identity": identity,
                "access_type": "ACCOUNT",
                "name": account.get("name"),
                "source": _name(account.get("source")),
                "description": "disabled" if account.get("disabled") else "enabled",
                "privileged": account.get("privileged"),
                "id": account.get("id"),
            }
        )
    return rows


def write_csv(rows: list[dict[str, Any]], identity: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", identity).strip("_") or "identity"
    path = output_dir / f"access_{slug}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    # utf-8-sig so Excel opens it with the right encoding.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and groupings the model needs to write a summary without the raw rows."""
    by_type: dict[str, int] = {}
    by_source: dict[str, dict[str, Any]] = {}
    privileged: list[dict[str, Any]] = []

    for row in rows:
        kind = row["access_type"]
        by_type[kind] = by_type.get(kind, 0) + 1
        if kind == "ACCOUNT":
            continue
        source = row.get("source") or "(no source)"
        bucket = by_source.setdefault(source, {"count": 0, "items": []})
        bucket["count"] += 1
        label = row["name"]
        if row.get("description"):
            label = f"{label} -- {row['description']}"
        bucket["items"].append(f"[{kind}] {label}")
        if row.get("privileged"):
            privileged.append(
                {"type": kind, "name": row["name"], "source": row.get("source")}
            )

    return {
        "counts_by_type": by_type,
        "by_source": by_source,
        "privileged": privileged,
        "direct_entitlements": sum(
            1 for r in rows if r.get("assignment") == "direct"
        ),
        "entitlements_via_bundles": sum(
            1 for r in rows if r.get("assignment") == "via bundle"
        ),
    }


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="analyze_identity_access")
    def analyze_identity_access(
        identity_id: str, output_dir: str | None = None
    ) -> dict[str, Any]:
        """Analyze everything one person can access and export it to a CSV.

        Use this for "what access does <person> have?". Do NOT guess the person:
        first call `search_identities` with their name. If more than one identity
        matches, show the user every candidate with name, job title, department
        and manager, and ask which one they mean. Only call this tool once the
        user has picked, passing that identity's `id`.

        The tool gathers roles, access profiles, entitlements and accounts, works
        out which bundle grants which entitlement, and flags privileged access. It
        writes every row to a CSV and returns a digest for you to turn into a
        short plain-English summary for a leadership audience: what the person can
        do, which systems they touch, anything privileged or unusual for their
        job title, and the CSV path.

        Args:
            identity_id: The `id` from a `search_identities` result.
            output_dir: Folder for the CSV. Defaults to the project's `output/`.

        Returns:
            `identity`, `csv_path`, `row_count`, and a `digest` with counts by
            type, a per-source breakdown of access items, privileged items and
            direct-vs-bundled entitlement counts. `notes` lists any enrichment
            that was skipped.
        """
        notes: list[str] = []

        try:
            document = fetch_identity_document(identity_id.strip())
        except Exception as exc:
            log.exception("analyze_identity_access: identity lookup failed")
            return {"error": describe_api_error(exc)}
        if not document:
            return {"error": f"No identity found with id {identity_id!r}."}

        access = [a for a in document.get("access") or [] if isinstance(a, dict)]
        profiles = [a for a in access if a.get("type") == "ACCESS_PROFILE"]
        roles = [a for a in access if a.get("type") == "ROLE"]

        expandable = [("profile", a) for a in profiles] + [("role", a) for a in roles]
        if len(expandable) > MAX_EXPANSIONS:
            notes.append(
                f"Only the first {MAX_EXPANSIONS} of {len(expandable)} roles/access "
                "profiles were expanded; 'granted_via' is incomplete."
            )
            expandable = expandable[:MAX_EXPANSIONS]

        # entitlement id -> names of the profiles/roles that bundle it
        bundles: dict[str, list[str]] = {}

        def expand(entry: tuple[str, dict[str, Any]]) -> None:
            kind, item = entry
            label = item.get("displayName") or item.get("name")
            try:
                if kind == "profile":
                    ids = [e.get("id") for e in _expand_access_profile(item["id"])]
                    for entitlement_id in ids:
                        bundles.setdefault(entitlement_id, []).append(label)
                else:
                    role = _expand_role(item["id"])
                    for ref in role.get("entitlements") or []:
                        bundles.setdefault(ref.get("id"), []).append(f"role: {label}")
                    for ref in role.get("access_profiles") or role.get("accessProfiles") or []:
                        for entitlement in _expand_access_profile(ref.get("id")):
                            bundles.setdefault(entitlement.get("id"), []).append(
                                f"role: {label} > {ref.get('name')}"
                            )
            except Exception as exc:
                log.warning("could not expand %s %s: %s", kind, label, exc)
                notes.append(f"Could not expand {kind} '{label}': {describe_api_error(exc)}")

        with ThreadPoolExecutor(EXPANSION_WORKERS) as pool:
            list(pool.map(expand, expandable))

        entitlement_ids = [a["id"] for a in access if a.get("type") == "ENTITLEMENT"]
        details: dict[str, dict[str, Any]] = {}
        if entitlement_ids:
            try:
                details = _fetch_entitlement_details(entitlement_ids)
            except Exception as exc:
                log.warning("entitlement enrichment failed: %s", exc)
                notes.append(
                    "Entitlement owner/privilege details unavailable: "
                    + describe_api_error(exc)
                )

        rows = build_rows(document, bundles, details)
        identity_name = document.get("displayName") or document.get("name") or identity_id
        attributes = document.get("attributes") or {}
        try:
            csv_path = write_csv(
                rows, identity_name, Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
            )
        except OSError as exc:
            return {"error": f"Could not write the CSV: {exc}"}

        return {
            "identity": {
                "id": document.get("id"),
                "name": identity_name,
                "email": document.get("email"),
                "job_title": attributes.get("jobTitle"),
                "department": attributes.get("department"),
                "manager": (document.get("manager") or {}).get("displayName"),
                "lifecycle_state": document.get("lifecycleState")
                or attributes.get("cloudLifecycleState"),
            },
            "csv_path": str(csv_path),
            "row_count": len(rows),
            "digest": summarize(rows),
            "notes": notes,
        }
