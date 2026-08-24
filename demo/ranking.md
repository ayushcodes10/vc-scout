# Investment ranking — ai-smb-ops-demo

15 of 15 candidate(s) in this run were analysed and have a memo.

- **Take a meeting:** 0
- **Watch:** 5
- **Pass:** 10

## The thesis being applied

> We invest in seed-stage, AI-native software companies that automate recurring, revenue-critical workflows for SMBs. The product should produce measurable value within 30 days, integrate into an existing system of record and develop defensibility through proprietary workflow data, distribution, integrations or operational depth rather than relying only on model access.

*Thesis version thesis_v1.*

## Rubric and thresholds

| Dimension | Max | What it asks |
| --- | ---: | --- |
| Pain and measurable ROI | 20 | Is the workflow recurring and revenue-critical, and can the buyer measure value within 30 days? |
| Product wedge | 15 | Is there a narrow, specific entry point that lands inside an existing system of record rather than asking for a workflow rewrite? |
| Distribution | 15 | Is there a credible, repeatable route to SMB buyers at acceptable cost? |
| Defensibility | 15 | Does an advantage compound through proprietary workflow data, distribution, integrations or operational depth rather than model access alone? |
| Team | 15 | Do the founders have earned insight into this workflow and the ability to ship? |
| Traction and freshness | 10 | Is there recent, verifiable evidence of customers, usage or revenue? |
| Market and timing | 10 | Why is this buyable now, and why was it not buildable before? |

- **Take a meeting** at 80/100 and above, **watch** from 65 to 79, **pass** below 65.
- Research confidence is **high** at 0.65 and above, **medium** from 0.40, **low** below that. It measures how much the research established, never how good the company is.
- A dimension's assessment status caps what it may score: supported 100%, partially supported 70%, not assessable 50%, contradicted 100%.
- Rubric 1.0.0, thesis thesis_v1.

## How to read a low score

A low score here means the evidence available did not support a higher one. It is not a finding that the company is weak. Where a dimension could not be assessed at all, the memo says so rather than scoring it as a failure.

The ordering below is **triage order** - which memo to open first - and not a quality ranking. A watch that exists only because the research came up short is not a judgement that the company is better than one that was passed on evidence.

## Ranking

| Rank | Company | Call | Score | Confidence | Thesis fit | Max achievable | Primary rationale | Memo |
| ---: | --- | --- | ---: | --- | --- | ---: | --- | --- |
| 1 | Dooza Desk | Watch | 26/100 | low | Not established by the sources | 50/100 | Insufficient evidence, not a judgement of the company | [Dooza Desk](memos/dooza-desk.md) |
| 2 | Inboto | Watch | 18/100 | low | Not established by the sources | 50/100 | Insufficient evidence, not a judgement of the company | [Inboto](memos/inboto.md) |
| 3 | gibsonai.com | Watch | 14/100 | low | Not established by the sources | 48/100 | Insufficient evidence, not a judgement of the company | [gibsonai.com](memos/gibsonai-com.md) |
| 4 | regulance.io | Watch | 14/100 | low | Not established by the sources | 48/100 | Insufficient evidence, not a judgement of the company | [regulance.io](memos/regulance-io.md) |
| 5 | SupportMatic | Watch | 10/100 | low | Not established by the sources | 50/100 | Insufficient evidence, not a judgement of the company | [SupportMatic](memos/supportmatic.md) |
| 6 | AI Agents for Customer Support | Pass | 42/100 | medium | Aligned with the thesis | 68/100 | Scored below the watch band on the evidence available | [AI Agents for Customer Support](memos/ai-agents-for-customer-support.md) |
| 7 | HeyDeacon | Pass | 39/100 | medium | Adjacent to the thesis | 65/100 | Scored below the watch band on the evidence available | [HeyDeacon](memos/heydeacon.md) |
| 8 | Luca | Pass | 39/100 | medium | Adjacent to the thesis | 67/100 | Scored below the watch band on the evidence available | [Luca](memos/luca.md) |
| 9 | Drafting AI | Pass | 35/100 | medium | Adjacent to the thesis | 65/100 | Scored below the watch band on the evidence available | [Drafting AI](memos/drafting-ai.md) |
| 10 | MCP Server for Appointment Booking | Pass | 34/100 | medium | Adjacent to the thesis | 72/100 | Scored below the watch band on the evidence available | [MCP Server for Appointment Booking](memos/mcp-server-for-appointment-booking.md) |
| 11 | ticketdesk.ai | Pass | 34/100 | medium | Adjacent to the thesis | 65/100 | Scored below the watch band on the evidence available | [ticketdesk.ai](memos/ticketdesk-ai.md) |
| 12 | Recursive | Pass | 29/100 | medium | Adjacent to the thesis | 65/100 | Scored below the watch band on the evidence available | [Recursive](memos/recursive.md) |
| 13 | helphubassistant.pages.dev | Pass | 28/100 | medium | Adjacent to the thesis | 62/100 | Scored below the watch band on the evidence available | [helphubassistant.pages.dev](memos/helphubassistant-pages-dev.md) |
| 14 | n8n.io | Pass | 28/100 | low | Outside the thesis, on evidence | 66/100 | Evidence places it outside the thesis | [n8n.io](memos/n8n-io.md) |
| 15 | SpecX | Pass | 21/100 | medium | Not established by the sources | 53/100 | Scored below the watch band on the evidence available | [SpecX](memos/specx.md) |

## Guardrail overrides

The deterministic policy moved or held a call in the following cases. A guardrail never raises a recommendation.

- **3x** The low score is driven by dimensions the evidence could not reach, so the policy holds at watch rather than reading a research shortfall as a judgement.
- **2x** No evidence claim could be extracted at all, so there is no basis for a positive or a negative call. The policy holds at watch rather than passing on an absence.

## Where the model and the policy disagreed

The analysis model's suggestion is recorded for evaluation and never consulted by the policy. It differed from the binding call here:

- **Dooza Desk** - the analysis model suggested pass; the policy decided watch.
- **Inboto** - the analysis model suggested pass; the policy decided watch.
- **gibsonai.com** - the analysis model suggested pass; the policy decided watch.
- **regulance.io** - the analysis model suggested pass; the policy decided watch.
- **SupportMatic** - the analysis model suggested pass; the policy decided watch.
- **AI Agents for Customer Support** - the analysis model suggested watch; the policy decided pass.
- **Drafting AI** - the analysis model suggested watch; the policy decided pass.

## Why no meeting recommendation

- No candidate in this run reached the take-a-meeting band at 80/100.
- Across 15 analysed candidate(s), the 105 scored dimension slots were assessed as: 8 supported, 30 partially supported, 4 contradicted, 63 not assessable.
- For 15 of 15 candidate(s), the assessment statuses recorded capped the achievable total below 80 before any judgement about the company; the highest achievable total in this run was 72/100.
- That is a statement about the evidence this run could gather, not a conclusion that these companies are uninvestable. No score has been raised to produce a meeting.
