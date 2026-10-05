from sailpoint_mcp.tools.access_requests import (
    classify_request,
    summarize_approval,
    summarize_request,
)


def _item(state, *approval_statuses, **extra):
    return {
        "state": state,
        "approval_details": [
            {"status": s, "current_owner": {"name": "Douglas.Flores"}}
            for s in approval_statuses
        ],
        **extra,
    }


def test_executing_with_pending_approver_is_pending_approval():
    assert classify_request(_item("EXECUTING", "PENDING")) == "pending_approval"


def test_executing_after_approval_is_in_progress():
    assert classify_request(_item("EXECUTING", "APPROVED")) == "in_progress"


def test_terminal_states_map_to_plain_buckets():
    assert classify_request(_item("REQUEST_COMPLETED")) == "completed"
    assert classify_request(_item("REJECTED")) == "rejected"
    assert classify_request(_item("PROVISIONING_FAILED")) == "failed"
    assert classify_request(_item("CANCELLED")) == "cancelled"


def test_summarize_request_names_who_is_holding_it_up():
    summary = summarize_request(
        _item(
            "EXECUTING",
            "PENDING",
            name="Create_Reports",
            requested_for={"name": "Adam.Kennedy"},
            requester={"name": "Janet.Washington"},
            requester_comment={"comment": "Needs it for month-end"},
        )
    )
    assert summary["status"] == "pending_approval"
    assert summary["waiting_on"] == ["Douglas.Flores"]
    assert summary["requested_for"] == "Adam.Kennedy"
    assert summary["requester_comment"] == "Needs it for month-end"


def test_lookup_error_in_reviewer_name_falls_back_to_owner():
    summary = summarize_approval(
        {
            "state": "APPROVED",
            "reviewedBy": {"name": "Failed to lookup MANAGER_OF ID: abc"},
            "owner": {"name": "hack.day"},
        }
    )
    assert summary["decided_by"] == "hack.day"


def test_summarize_approval_reads_camel_case_and_drops_empties():
    summary = summarize_approval(
        {
            "id": "a1",
            "state": "APPROVED",
            "requestedObject": {"name": "Create_Reports", "type": "ENTITLEMENT"},
            "requestedFor": {"name": "Adam.Kennedy"},
            "reviewedBy": {"name": "Douglas.Flores"},
            "modified": "2026-10-01T10:00:00Z",
        }
    )
    assert summary["item"] == "Create_Reports"
    assert summary["decided_by"] == "Douglas.Flores"
    assert "reviewer_comment" not in summary
