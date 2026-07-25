# Case Study — AgencyOps Orchestrator

**Agentic automation for an eCommerce marketing agency**

Hamza Aziz · AI Product Engineer
LangGraph · FastAPI · Google Gemini · Python

> **Provenance, stated plainly:** this is a self-directed build responding to a
> real published job specification, not a delivered client engagement. The
> requirements are the client's; the architecture, code and engineering
> decisions are mine. Say it this way in interviews — the work stands on its
> own and the honesty is worth more than the inflation.

---

## The brief

A remote eCommerce marketing agency posted for an AI automation consultant.
Their stated need:

> *"Design and build automated workflows connecting Claude, ChatGPT, Zapier,
> Trello, Slack, Harvest, WhatsApp and Meta Ads … automate recurring internal
> processes (weekly client reporting, Harvest time-tracking workflows,
> AI-generated creative pipelines) … document workflows clearly so our internal
> team can maintain them."*

The obvious response is a set of Zapier chains. I took the brief as an
engineering problem instead, because the interesting constraint is buried in
the last line: *the agency has to be able to maintain and trust this.*

---

## The problem with the obvious solution

I mapped the two highest-value processes in the brief — weekly client reporting
and creative production — as linear automation chains, then asked what breaks
when they touch a paying client.

**1. The LLM does arithmetic.**
A reporting chain that hands raw campaign data to a model and asks for a
summary is asking the model to both compute and narrate. Models are good at
one of those. A hallucinated ROAS figure in a client-facing report is not a
degraded experience; it is a lost account.

**2. Brand rules live in a prompt.**
"Don't use these phrases" in a system prompt is a request, not a constraint.
For an agency running paid social, off-brand or non-compliant copy going live
is a real commercial and regulatory exposure — particularly in fitness and
health verticals where outcome claims are restricted.

**3. Nothing can be paused.**
A Zapier chain either completes or fails. There is no state in which the work
is done, the output is drafted, and a human is deciding whether to release it.
For anything client-facing, that state is the whole job.

These three are one architectural problem: **the system conflates deciding
what to do with actually doing it.**

---

## What I built

A LangGraph-based orchestrator where workflows *propose* external writes and a
separate dispatcher *executes* them, only after approval.

### Separation of concerns, enforced structurally

Each graph node has a single permitted capability:

| Node | May do | May not do |
|---|---|---|
| `gather` | Call connectors | Contain logic |
| `analyse` | Deterministic maths | Call an LLM |
| `narrate` | Call an LLM | Compute a number |
| `propose` | Construct `Effect` objects | Execute anything |
| `dispatch` | Mutate external systems | Anything else |

Every figure in a client report is computed in `analysis.py` — plain Python,
fully unit-tested. The LLM receives those computed figures and writes prose
about them. It is never in a position to invent one. There is a test asserting
that the totals appearing in the narrative match the totals computed in code.

### The Effect model

```python
@dataclass
class Effect:
    connector: str          # "slack"
    action: str             # "post_message"
    payload: dict           # what would be sent
    summary: str            # human-readable description
    reversible: bool        # a Slack post cannot be unsent
    status: str             # proposed → approved → executed
```

Workflows return a list of these. Constructing one cannot cause a side effect —
a test asserts that a full reporting run with the gate on results in exactly
zero API calls. A human then releases them, all or in part:

```bash
# Release the client report, hold the internal action cards
POST /runs/{id}/decision {"decision":"approve","effect_indexes":[0]}
```

This is the piece a Zapier chain structurally cannot provide.

### The bounded revision loop

The creative pipeline scores generated ad copy against machine-checkable brand
rules, then routes failures back to the model **with their specific violations
attached**:

```
score          2/6 passed brand check
revise_round_1 revised 4 failing variants
score          5/6 passed brand check
revise_round_2 revised 1 failing variants
score          5/6 passed brand check
select         5 approved, 1 rejected after 2 revision rounds
```

The loop is bounded. Copy that cannot be brought into compliance is escalated
to a human copywriter rather than retried indefinitely — an automation that
fails loudly is worth more than one that burns tokens quietly.

**A bug worth keeping:** an early revision pass turned *"cheap, fast and the
best price ever"* into *"fast and the"*. That output passed the banned-phrase
check and the character limit while being unreadable. I added a coherence
heuristic that detects clauses ending on dangling function words and mid-word
truncation. The lesson generalises: **automated repair needs an automated
sanity check behind it**, or you have built a machine for producing plausible
garbage.

---

## Engineering decisions worth defending

**Deterministic offline LLM engine.**
Behind the same interface as Gemini sits an engine that produces genuine,
data-grounded output with no API key. This is not a test mock. It makes the
demo runnable anywhere, the 48-test suite deterministic and free, and — the
actual production argument — it means an LLM outage degrades reporting to
templated prose instead of taking client reporting offline entirely.

**Connectors behind protocols.**
Graphs talk to `AdsConnector`, never to the Meta SDK. Mock and live
implementations satisfy the same contract and raise the same errors, so the
entire test suite exercises real graph logic. Swapping to live credentials is a
config change. An agency that later moves off Harvest rewrites one file.

**Materiality thresholds.**
Movements under 10%, or on campaigns spending under AED 2,000, never surface as
findings. This is a product decision, not a technical one: an automated report
that flags everything gets ignored inside a fortnight, and an ignored report is
worse than no report because someone is still paying for it.

**Tracing as a plain JSON file.**
No vendor tracing SDK. Every node records a step; every run persists as JSON
the agency owns. When someone asks in six months why a campaign got paused,
the answer is on disk and readable without a subscription.

---

## Results

| | |
|---|---|
| Workflows | 2 end-to-end (reporting, creative production) |
| Test suite | 48 tests, all passing |
| External writes made without approval | 0 — asserted by test |
| Runs with no credentials | Full demo + entire suite |
| Connectors | 4 (Meta Ads, Harvest, Slack, Trello) — mock + live |

**Reporting time.** The manual version of the weekly report is roughly 90
minutes per client per week: pull Meta figures, pull Harvest hours, build the
table, write commentary, chase the account lead. The orchestrated version runs
in under a second and stops at the review step. Across an agency carrying ten
retainers that is the difference between a full day of analyst time weekly and
a review pass over pre-drafted output.

---

## What I would do next

- **Durable checkpointer.** Paused runs are in memory; LangGraph's Postgres
  checkpointer drops in without touching node code, since the graphs are
  already written as pure state transitions.
- **Auth on the approval endpoint.** The gate is a security boundary — it
  needs workspace-scoped tokens and an audit trail of who released what.
- **WhatsApp Business connector.** In the original brief, not built. It is a
  `WriteConnector` subclass against the Cloud API; the interface already
  accommodates it.
- **Evaluation harness for the creative pipeline.** Score-vs-human-judgement
  agreement, to know whether the brand rules are actually catching what a
  copy lead would catch.

---

## Talking points

**Why LangGraph over a plain script or Zapier?**
The approval gate. A graph can halt mid-run with state intact and resume after
a human decision. A linear chain either completes or fails; there is no pause.
That single requirement drives the architecture.

**How do you stop the model hallucinating client numbers?**
Structurally, not with prompting. The model never computes. `analysis.py` does
the arithmetic in tested Python; the model receives the results and writes
prose about them. A test asserts the figures in the narrative match the figures
in the totals.

**What happens when the LLM is down?**
The offline engine takes over and reporting continues with templated
commentary. Degraded, not broken. That is why the fallback is a real engine
rather than a test stub.

**What is the weakest part?**
The brand-compliance scoring is heuristic. It catches banned phrases, limits
and grammatical breakage, but it will not catch copy that is technically
compliant and tonally wrong. That is exactly why unfixable variants escalate to
a human instead of being silently dropped — the system is designed around
knowing what it cannot judge.
