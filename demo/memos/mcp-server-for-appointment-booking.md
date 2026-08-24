# MCP Server for Appointment Booking

**Pass** · 34/100 · medium confidence

**One-sentence call:** Pass: MCP Server for Appointment Booking scored 34/100 against the thesis rubric on medium-confidence research, with thesis fit recorded as adjacent - short of what a meeting needs.

## Snapshot

| Field | Value |
| --- | --- |
| Website | <https://blog.makeplans.com/> |
| Discovered via | [S1] [S2] |
| Product in plain language | MakePlans is an existing appointment booking and event registration platform (with API and integrations) that has added an MCP (Model Context Protocol) server, allowing AI assistants like Claude or ChatGPT… |
| Buyer | Not established by the sources |
| Workflow | Not established by the sources |
| Thesis fit | Adjacent to the thesis |
| Final recommendation | Pass |
| Model suggestion | Pass |
| Research confidence | medium (0.55 of 1.00) |
| Maximum achievable score | 72/100 under the recorded assessment statuses - the take-a-meeting band at 80 was out of reach on this evidence |

## Why this call

This is a pass on thesis fit at the score the evidence supported. It is not a finding that the company is failing.

- Scored 34/100 against the rubric, which falls in the 0-64 band (pass).
- Research confidence is medium (0.55).
- 2 dimension(s) could not be assessed from the available evidence: distribution, team. Only 70 of 100 points were assessable.
- **Thesis fit.** The workflow (appointment booking) is recurring and plausibly revenue-critical for SMBs, and the product integrates into an existing system of record (the business's own MakePlans account) rather than requiring a… [S3] [S2] [S1]

## Investment view

### Team

No individuals, founders, or their backgrounds are named in the dossier; only the company's location (Norway) is stated. [S3]

### Product

MakePlans is a broader appointment booking and event registration system with an API and integrations; it has added an open-protocol MCP server that lets AI assistants check availability, list services, look up customers, and create/cancel bookings using the business's own API credentials, as an addition rather than replacement to the existing product. [S3] [S2]

### Market

No market sizing, competitive positioning, or demand evidence is supplied in the dossier. _Open question_

## Scorecard

| Dimension | Score | Assessment | Rationale | Sources |
| --- | ---: | --- | --- | --- |
| Pain and measurable ROI | 8 / 20 | Partially supported | Appointment booking is plausibly a recurring, revenue-critical SMB workflow, and the company names the workflow… | [S3] [S2] |
| Product wedge | 10 / 15 | Supported | The company gives concrete, specific facts: the MCP server authenticates with the business's own existing… | [S3] [S2] |
| Distribution | 3 / 15 | Not assessable | No go-to-market motion, channel, self-serve signup, or pricing details are provided beyond the existence of… | _Open question_ |
| Defensibility | 4 / 15 | Contradicted | The company explicitly states it built on an open protocol (MCP) usable by any assistant,… | [S2] |
| Team | 3 / 15 | Not assessable | No founders or team members are named; only the company's Norway location is stated, which… | [S3] |
| Traction and freshness | 2 / 10 | Partially supported | The only independent signal is a Show HN thread with 1 point and 1 comment,… | [S1] |
| Market and timing | 4 / 10 | Partially supported | The rise of MCP as an emerging standard for AI-assistant tool access is implicit in… | [S2] |
| **Total** | **34 / 100** | 70 of 100 points were assessable | Maximum achievable under these statuses: 72/100. |  |

## Key risks and open questions

- The only independent traction signal (Show HN) shows minimal engagement (1 point, 1 comment), indicating essentially no external attention detected. [S1]
- No independent user reports exist about technical limitations, security issues, or failure cases of the MCP server. _Open question_
- Who founded or works at MakePlans, and what is their background? _Open question_
- How many businesses or customers use MakePlans or its MCP server, and what usage volumes exist? _Open question_
- What is the size of the appointment-booking market or MakePlans' competitive position within it? _Open question_

## What would change our mind

- Evidence of actual paying customers or usage volume of the MCP server beyond the company's own description
- Independent validation of measurable time or revenue savings from AI-assisted booking management within a defined period
- Information on the founding team's background and execution track record

## Sources

**[S1]** Show HN: MCP Server for Appointment Booking · Hacker News launch thread · <https://news.ycombinator.com/item?id=49326992> · observed 2026-08-24
  > The thread has 1 points on Hacker News. The thread has 1 comments…
**[S2]** Your AI assistant can now manage your bookings - We Make Plans · launch page posted to Hacker News · <https://blog.makeplans.com/2026/08/15/your-ai-assistant-can-now-manage-your-bookings.html> · observed 2026-08-24
  > Once connected, your assistant can work with your MakePlans account: Check availability and…
**[S3]** We Make Plans - the MakePlans blog · company homepage · <https://blog.makeplans.com/> · observed 2026-08-24
  > Once connected, your assistant can work with your MakePlans account: Check availability and…

## Generation note

Evidence extracted by anthropic/claude-sonnet-5 (evidence_v1); analysed by anthropic/claude-sonnet-5 (analysis_v2). Thesis thesis_v1, rubric 1.0.0, policy 2.0.0. The total was recomputed in Python and the recommendation was made by the deterministic policy, not by the model.
