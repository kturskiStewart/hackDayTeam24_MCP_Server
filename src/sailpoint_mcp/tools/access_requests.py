"""Access request tracking, from both sides of the request.

  * `get_access_request_status` -- the requester / beneficiary view. Where did the
    request for me (or my report) get to? Pending approval, in progress, done,
    rejected, failed? In setups where only managers can request access, the person
    who needs the access never sees the ticket, so this is how they find out.
  * `get_approvals` -- the approver view. What is waiting on me, and what did I
    (or someone) recently decide?

Both take an identity `id` from `search_identities`, because the server runs as a
service account rather than as the person asking. Dates are filtered server-side
with a `created`/`modified` bound so a long history does not flood the model.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from mcp.server import MCPServer
from sailpoint import AccessRequestApprovalsApi, AccessRequestsApi

from ..client import call_sailpoint, describe_api_error

log = logging.getLogger(__name__)

MAX_LIMIT = 250
DEFAULT_DAYS = 30
MAX_DAYS = 365

# Which side of the request the identity is on, for `get_access_request_status`.
DIRECTIONS = {
    "for": "requested_for",  # requests made on this person's behalf
    "by": "requested_by",  # requests this person submitted (e.g. a manager)
    "either": "regarding_identity",
}


def _plain(value: Any) -> Any:
    """Enums to their values, datetimes to ISO strings; leave the rest alone."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _ref_name(ref: Any) -> str | None:
    """Name out of a `{id, name, type}` reference."""
    return ref.get("name") if isinstance(ref, dict) else None


def _since(days: int) -> str:
    """ISO timestamp `days` ago for a V3 date filter. It must NOT be quoted: a
    quoted date parses fine but silently matches nothing."""
    days = max(1, min(days, MAX_DAYS))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def classify_request(item: dict[str, Any]) -> str:
    """One plain-language bucket for a request item, so the model need not decode
    ISC's state machine.

    An item in `EXECUTING` is either waiting on a human approver or being
    provisioned; the per-approver `status` tells them apart.
    """
    state = _plain(item.get("state"))
    approvals = item.get("approval_details") or item.get("approvalDetails") or []
    if state == "EXECUTING":
        if any(_plain(a.get("status")) == "PENDING" for a in approvals):
            return "pending_approval"
        return "in_progress"
    return {
        "REQUEST_COMPLETED": "completed",
        "REJECTED": "rejected",
        "CANCELLED": "cancelled",
        "TERMINATED": "cancelled",
        "PROVISIONING_VERIFICATION_PENDING": "in_progress",
        "PROVISIONING_FAILED": "failed",
        "NOT_ALL_ITEMS_PROVISIONED": "failed",
        "ERROR": "failed",
    }.get(state, "unknown")


def summarize_request(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten a RequestedItemStatus into what someone asking "what's the status?"
    needs: what, for whom, who asked, who is holding it up, and how old it is."""
    approvals = item.get("approval_details") or item.get("approvalDetails") or []
    waiting_on = sorted(
        {
            _ref_name(a.get("current_owner") or a.get("currentOwner"))
            for a in approvals
            if _plain(a.get("status")) == "PENDING"
        }
        - {None}
    )
    decided = {
        decision: sorted(
            {
                _ref_name(a.get("current_owner") or a.get("currentOwner"))
                for a in approvals
                if _plain(a.get("status")) == decision
            }
            - {None}
        )
        for decision in ("APPROVED", "REJECTED")
    }
    comment = item.get("requester_comment") or item.get("requesterComment") or {}
    errors = [
        message.get("text")
        for group in item.get("error_messages") or item.get("errorMessages") or []
        for message in (group if isinstance(group, list) else [group])
        if isinstance(message, dict) and message.get("text")
    ]

    # Before any human approver exists a request sits in an automated phase
    # (e.g. SOD_PHASE); say which, so "in progress" is not a dead end.
    phases = item.get("access_request_phases") or item.get("accessRequestPhases") or []
    current_phase = next(
        (p.get("name") for p in phases if not p.get("finished")), None
    )

    summary = {
        "request_id": item.get("access_request_id") or item.get("accessRequestId"),
        "item": item.get("name"),
        "item_type": _plain(item.get("type")),
        "request_type": _plain(item.get("request_type") or item.get("requestType")),
        "status": classify_request(item),
        "state": _plain(item.get("state")),
        "requested_for": _ref_name(item.get("requested_for") or item.get("requestedFor")),
        "requested_by": _ref_name(item.get("requester")),
        "description": item.get("description"),
        "waiting_on": waiting_on,
        "approved_by": decided["APPROVED"],
        "rejected_by": decided["REJECTED"],
        "current_phase": current_phase,
        "requester_comment": comment.get("comment") if isinstance(comment, dict) else None,
        "errors": errors,
        "created": _plain(item.get("created")),
        "modified": _plain(item.get("modified")),
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}


def pending_approvers(request_ids: list[str]) -> dict[str, list[str]]:
    """Who each pending request is waiting on, straight from the approvals API.

    The request-status record only names an approver for single-person schemes.
    For "all owners" or group approval it has no name at all, while the approval
    itself always carries the real owner -- so ask it. Needs an org-admin PAT to
    see other people's approvals.
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


def add_pending_approvers(requests: list[dict[str, Any]]) -> None:
    """Fill `waiting_on` for pending requests, in place. Never raises: the
    caller's answer is still useful without names, so a failed lookup is logged."""
    pending = [r for r in requests if r.get("status") == "pending_approval"]
    try:
        approvers = pending_approvers([r["request_id"] for r in pending if r.get("request_id")])
    except Exception as exc:
        log.warning("could not look up pending approvers: %s", exc)
        return
    for request in pending:
        names = approvers.get(request.get("request_id"))
        if names:
            request["waiting_on"] = sorted(set(request.get("waiting_on", [])) | set(names))


def _reviewer_name(item: dict[str, Any]) -> str | None:
    """Who decided a completed approval.

    ISC sometimes puts a lookup error ("Failed to lookup MANAGER_OF ID: ...") in
    `reviewedBy.name` for approvals routed by approver type. That is not a name,
    so fall back to the approval's owner, who is who actually decided it.
    """
    reviewer = _ref_name(item.get("reviewed_by") or item.get("reviewedBy"))
    if reviewer and not reviewer.startswith("Failed to lookup"):
        return reviewer
    return _ref_name(item.get("owner"))


def summarize_approval(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten a pending or completed approval."""
    requested = item.get("requested_object") or item.get("requestedObject") or {}
    comment = item.get("requester_comment") or item.get("requesterComment") or {}
    reviewer_comment = item.get("reviewer_comment") or item.get("reviewerComment") or {}
    summary = {
        "approval_id": item.get("id"),
        "request_id": item.get("access_request_id") or item.get("accessRequestId"),
        "item": requested.get("name") if isinstance(requested, dict) else None,
        "item_type": requested.get("type") if isinstance(requested, dict) else None,
        "item_description": requested.get("description")
        if isinstance(requested, dict)
        else None,
        "request_type": _plain(item.get("request_type") or item.get("requestType")),
        "requested_for": _ref_name(item.get("requested_for") or item.get("requestedFor")),
        "requested_by": _ref_name(item.get("requester")),
        "decision": _plain(item.get("state")),
        "decided_by": _reviewer_name(item),
        "requester_comment": comment.get("comment") if isinstance(comment, dict) else None,
        "reviewer_comment": reviewer_comment.get("comment")
        if isinstance(reviewer_comment, dict)
        else None,
        "privileged": item.get("privilege_level") or item.get("privilegeLevel"),
        "requested_on": _plain(
            item.get("request_created") or item.get("requestCreated") or item.get("created")
        ),
        "decided_on": _plain(item.get("modified")) if item.get("state") else None,
    }
    return {key: value for key, value in summary.items() if value not in (None, [], "")}


def register(mcp: MCPServer) -> None:
    @mcp.tool(name="get_access_request_status")
    def get_access_request_status(
        identity_id: str,
        direction: str = "for",
        days: int = DEFAULT_DAYS,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Show the status of access requests involving one person.

        Use for "is there a pending access request for me?", "what's the status
        of the access my manager asked for?", "what was recently approved?" or
        "what requests did I submit for my team?". Find the person's `id` with
        `search_identities` first (confirm which person if several match).

        Args:
            identity_id: The identity's `id`.
            direction: `for` (default) = requests made on this person's behalf;
                `by` = requests this person submitted, e.g. a manager asking for
                access for their reports; `either` = both.
            days: Look back this many days by creation date. Default 30, max 365.
            status: Optional filter on the plain-language status: `pending_approval`,
                `in_progress`, `completed`, `rejected`, `failed` or `cancelled`.
            limit: Maximum requests to fetch, 1-250. Default 50.

        Returns:
            `requests` (newest first), each with item, requested_for, requested_by,
            `status`, who it is `waiting_on`, and dates; plus `counts_by_status`.
            For a pending request, tell the user who is holding it up.
        """
        param = DIRECTIONS.get(direction.lower())
        if param is None:
            return {"error": f"direction must be one of {sorted(DIRECTIONS)}."}
        limit = max(1, min(limit, MAX_LIMIT))

        try:
            items = call_sailpoint(
                lambda client: AccessRequestsApi(client).list_access_request_status_v1(
                    **{param: identity_id},
                    limit=limit,
                    filters=f"created ge {_since(days)}",
                    sorters="-created",
                )
            )
        except Exception as exc:
            log.exception("get_access_request_status failed")
            return {"error": describe_api_error(exc)}

        requests = [summarize_request(item.to_dict()) for item in items or []]
        add_pending_approvers(requests)
        if status:
            requests = [r for r in requests if r.get("status") == status]

        response: dict[str, Any] = {
            "identity_id": identity_id,
            "direction": direction,
            "days": max(1, min(days, MAX_DAYS)),
            "returned": len(requests),
            "counts_by_status": dict(Counter(r.get("status") for r in requests)),
            "requests": requests,
        }
        if not requests:
            response["note"] = (
                f"No matching access requests in the last {response['days']} days."
            )
        elif len(items) == limit:
            response["note"] = f"Hit the limit of {limit}; older requests may exist."
        return response

    @mcp.tool(name="get_approvals")
    def get_approvals(
        approver_id: str,
        include_completed: bool = False,
        days: int = DEFAULT_DAYS,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List the access approvals waiting on an approver, and optionally recent decisions.

        Use for "do I still need to approve anything?", "what's waiting on
        <manager>?" and "what did I approve recently?". Find the approver's `id`
        with `search_identities` first.

        Args:
            approver_id: The `id` of the approver (the owner of the approvals).
            include_completed: Also return approvals decided in the last `days`
                days. Default False (pending only).
            days: Look-back window for completed approvals. Default 30, max 365.
            limit: Maximum items per list, 1-250. Default 50.

        Returns:
            `pending` (oldest first, so the most overdue is on top) and, if asked,
            `completed` with the decision, plus counts.
        """
        limit = max(1, min(limit, MAX_LIMIT))

        try:
            pending = call_sailpoint(
                lambda client: AccessRequestApprovalsApi(client).list_pending_approvals_v1(
                    owner_id=approver_id, limit=limit
                )
            )
            completed = []
            if include_completed:
                completed = call_sailpoint(
                    lambda client: AccessRequestApprovalsApi(
                        client
                    ).list_completed_approvals_v1(
                        owner_id=approver_id,
                        limit=limit,
                        filters=f"modified ge {_since(days)}",
                        sorters="-modified",
                    )
                )
        except Exception as exc:
            log.exception("get_approvals failed")
            return {"error": describe_api_error(exc)}

        pending_items = sorted(
            (summarize_approval(item.to_dict()) for item in pending or []),
            key=lambda item: item.get("requested_on") or "",
        )
        response: dict[str, Any] = {
            "approver_id": approver_id,
            "pending_count": len(pending_items),
            "pending": pending_items,
        }
        if include_completed:
            decided = [summarize_approval(item.to_dict()) for item in completed or []]
            response["days"] = max(1, min(days, MAX_DAYS))
            response["completed_count"] = len(decided)
            response["completed"] = decided
        if not pending_items:
            response["note"] = "Nothing is waiting on this approver."
        return response
