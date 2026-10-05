# Five-minute demo script

Everything below runs live against the demo tenant through an MCP client (Claude
Code). Open [the examples](examples/) in a browser tab beforehand as a fallback.

**Before you start:** the `sailpoint` server shows as connected in `/mcp`, and
`python scripts/check_auth.py "Adam Kennedy"` prints three `OK` lines.

## 1. Executive access review (~1.5 min)

> "What access does Adam Kennedy have?"

Search finds Adam, the assistant gathers his roles, access profiles, entitlements
and accounts, writes a CSV, and summarizes it in plain language. Open the CSV.

*To show disambiguation:* ask about a first name several people share. The
assistant lists each candidate with job title and manager and asks which one.

## 2. Access request tracking (~2 min)

> "Is there any pending access request for Adam Kennedy?"

Shows what Adam can't see himself: requests made on his behalf and who is holding
them up.

> "Give me a report of Adam's access requests."

Open the HTML sheet. Pending items come first. Read out the
**Ultimate Universe Destroyer Beholder Role** row: its description says "DO NOT
approve any requests for this role", and the sheet shows it waiting on an approver.
Then point at the approved role and the automatically granted profile.

> "Does hack.day still need to approve anything?" / "What did hack.day approve
> recently?"

The approver's view of the same workflow.

## 3. Terminated users with live access (~1 min)

> "How many terminated users still have active access?"

Headline numbers first (9 leavers, all with enabled accounts, 27 accounts), then
open the HTML report and the CSV for follow-up.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| Tool not called | `/mcp` -> check `sailpoint` is connected; reload the window |
| `CERTIFICATE_VERIFY_FAILED` | Netskope: see README troubleshooting |
| Token error | `SAIL_BASE_URL` needs `.api.` in the host |
| Empty request lists | Dates are filtered server-side; widen with `days` |
| Need fresh evidence | `python scripts/generate_examples.py` |
