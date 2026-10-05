# Five-minute demo script

Everything below runs live against the demo tenant through an MCP client (Claude
Code). Open [the examples](examples/) in a browser tab beforehand as a fallback.

**Before you start:** the `sailpoint` server shows as connected in `/mcp`, and
`python scripts/check_auth.py "Adam Kennedy"` prints three `OK` lines.

## Open (10 seconds)

> "Ask your SailPoint tenant a question in plain English and get back a report you
> can hand to someone. Three questions that normally need an expert."

Say that, then go straight to the first demo. Don't mention the joke role yet.

## 1. "Is my access request stuck?" (~2 min) -- the fun moment and the gap

The gap: the person who needs the access can't see the ticket.

> "Is there any pending access request for Adam Kennedy?"

Shows what Adam can't see himself: requests made on his behalf and who is holding
them up.

> "Give me a report of Adam's access requests."

Open the HTML sheet. Pending items come first. Read out the
**Ultimate Universe Destroyer Beholder Role** row: its description says "DO NOT
approve any requests for this role", and the sheet shows it waiting on an approver.
Pause for the laugh, then point at the approved role and the automatically granted
profile.

> "Does hack.day still need to approve anything?" / "What did hack.day approve
> recently?"

The approver's view of the same workflow. Skip if short on time.

## 2. "Which terminated users still have live access?" (~1.5 min) -- the payoff

> "How many terminated users still have active access?"

Headline numbers first (9 leavers, all with enabled accounts, 27 accounts), then
open the HTML report. Say who asks this: a security manager before an audit.

## 3. "What access does this person have?" (~1 min) -- short closer

> "What access does Adam Kennedy have?"

The assistant gathers his roles, access profiles, entitlements and accounts,
writes a CSV, and summarizes it in plain language. Open the CSV.

*To show disambiguation, if there is time:* ask about a first name several people
share. The assistant lists each candidate with job title and manager and asks
which one.

## Close (10 seconds)

> "Same server, three questions, and each answer is a file you can send."

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| Tool not called | `/mcp` -> check `sailpoint` is connected; reload the window |
| `CERTIFICATE_VERIFY_FAILED` | Netskope: see README troubleshooting |
| Token error | `SAIL_BASE_URL` needs `.api.` in the host |
| Empty request lists | Dates are filtered server-side; widen with `days` |
| Need fresh evidence | `python scripts/generate_examples.py` |
