#!/usr/bin/env python3
"""Run each use-case tool against the live tenant and save the output as evidence.

    python scripts/generate_examples.py

Writes to docs/examples/. Re-run it whenever a tool changes so the examples in
docs/use-cases.md stay real. Everything here is read-only against the tenant.
The tool output is whatever the tenant returns, so only run it against a demo or
test tenant, or review the files before committing them.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sailpoint_mcp.tools import (  # noqa: E402
    access_request_report,
    access_requests,
    analyze_identity_access,
    terminated_access_audit,
)
from sailpoint_mcp.tools.search_identities import (  # noqa: E402
    build_query_string,
    summarize_identity,
)

EXAMPLES = ROOT / "docs" / "examples"

# (label, name to search for) -- resolved to an id at run time so the script does
# not hard-code tenant ids.
PEOPLE = ["Adam Kennedy", "Janet Washington"]
APPROVER = "hack.day"


class _Collector:
    """Stands in for MCPServer so the registered tool functions can be called."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, name: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def _save(name: str, payload) -> None:
    path = EXAMPLES / name
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def _find(name: str) -> dict | None:
    from sailpoint import SearchApi
    from sailpoint.search.models.query import Query
    from sailpoint.search.models.search import Search

    from sailpoint_mcp.client import call_sailpoint

    search = Search(indices=["identities"], query=Query(query=build_query_string(name)))
    hits = call_sailpoint(lambda c: SearchApi(c).search_post_v1(search=search, limit=1))
    return summarize_identity(hits[0]) if hits else None


def main() -> int:
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    collector = _Collector()
    analyze_identity_access.register(collector)
    access_requests.register(collector)
    access_request_report.register(collector)
    terminated_access_audit.register(collector)

    _save("_generated.json", {"generated_at": datetime.now(timezone.utc).isoformat()})

    for person in PEOPLE:
        identity = _find(person)
        if not identity:
            print(f"skip {person}: not found")
            continue
        slug = person.lower().replace(" ", "_")
        _save(f"identity_{slug}.json", identity)

        result = collector.tools["analyze_identity_access"](
            identity["id"], output_dir=str(EXAMPLES / "_tmp")
        )
        csv_path = result.pop("csv_path", None)
        if csv_path:
            shutil.copy(csv_path, EXAMPLES / f"access_{slug}.csv")
            print(f"wrote docs/examples/access_{slug}.csv")
        _save(f"analyze_identity_access_{slug}.json", result)

        _save(
            f"access_request_status_{slug}.json",
            collector.tools["get_access_request_status"](identity["id"], direction="either"),
        )
        _save(
            f"approvals_{slug}.json",
            collector.tools["get_approvals"](identity["id"], include_completed=True),
        )

        report = collector.tools["export_access_request_report"](
            identity["id"], output_dir=str(EXAMPLES / "_tmp")
        )
        for kind in ("csv", "html"):
            path = report.pop(f"{kind}_path", None)
            if path:
                shutil.copy(path, EXAMPLES / f"access_requests_{slug}.{kind}")
                print(f"wrote docs/examples/access_requests_{slug}.{kind}")
        _save(f"access_request_report_{slug}.json", report)

    audit = collector.tools["audit_terminated_access"](output_dir=str(EXAMPLES / "_tmp"))
    for kind in ("csv", "html"):
        path = audit.pop(f"{kind}_path", None)
        if path:
            shutil.copy(path, EXAMPLES / f"terminated_access.{kind}")
            print(f"wrote docs/examples/terminated_access.{kind}")
    _save("audit_terminated_access.json", audit)

    # The approver side: whoever approved the demo requests (the PAT's own identity).
    approver = _find(APPROVER)
    if approver:
        _save(
            "approvals_hack_day.json",
            collector.tools["get_approvals"](approver["id"], include_completed=True),
        )

    shutil.rmtree(EXAMPLES / "_tmp", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
