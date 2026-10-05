from datetime import datetime, timezone

from sailpoint_mcp.tools.access_request_report import build_report_rows, render_html

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _request(status, **extra):
    return {
        "request_id": "r1",
        "item": "Some Access",
        "item_type": "ACCESS_PROFILE",
        "status": status,
        "requested_by": "hack.day",
        "created": "2026-10-05T18:00:00+00:00",
        **extra,
    }


def test_pending_row_names_the_approver_and_comes_first():
    rows = build_report_rows(
        [
            _request("completed", description="Approved thing", approved_by=["Ann"]),
            _request(
                "pending_approval",
                description="Lets you do the thing",
                waiting_on=["Tyler", "Sam"],
            ),
        ],
        now=NOW,
    )
    assert rows[0]["section"] == "pending_approval"
    assert rows[0]["approver"] == "Tyler and Sam"
    assert rows[0]["summary"] == "Lets you do the thing. Waiting on Tyler and Sam to approve."
    assert rows[0]["days_open"] == 4


def test_completed_row_says_who_approved_or_that_none_was_needed():
    approved, auto = build_report_rows(
        [
            _request("completed", description="A.", approved_by=["Ann"]),
            _request("completed", description="B."),
        ],
        now=NOW,
    )
    assert approved["summary"].endswith("Approved by Ann and granted.")
    assert auto["summary"].endswith("Granted automatically; no approval was required.")


def test_missing_description_and_phase_are_handled():
    row = build_report_rows(
        [_request("in_progress", current_phase="SOD_PHASE")], now=NOW
    )[0]
    assert row["summary"].startswith("No description was provided")
    assert "separation-of-duties" in row["summary"]


def test_rejected_goes_in_not_granted():
    row = build_report_rows([_request("rejected", rejected_by=["Bob"])], now=NOW)[0]
    assert row["section"] == "not_granted"
    assert row["summary"].endswith("Rejected by Bob.")


def test_html_escapes_content():
    rows = build_report_rows(
        [_request("pending_approval", item="<script>x</script>", waiting_on=["A"])],
        now=NOW,
    )
    page = render_html("Adam <b>", 30, rows)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
