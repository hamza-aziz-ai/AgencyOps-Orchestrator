# AgencyOps Orchestrator

Agentic workflow automation for an eCommerce marketing agency — built on
LangGraph, FastAPI and Ollama (gpt-oss).

It automates two recurring agency processes end to end (weekly client
reporting and creative production), and it does so with a property most
no-code automation lacks: **nothing reaches a client without a human
releasing it.**

```
$ python scripts/demo.py          # no API keys required
$ python -m pytest -q             # 101 passed
$ uvicorn agencyops.api.main:app --app-dir src   # console at localhost:8000
```

---

## The problem this solves

Marketing agencies automate with Zapier chains: trigger → step → step → send.
That works until the automation touches a client. Then three things break:

| Failure | Why it happens | What this does instead |
|---|---|---|
| A wrong number reaches the client | LLM asked to both compute *and* narrate | Maths in Python, prose from the LLM. The model never calculates. |
| Off-brand ad copy goes live | Brand rules live in a prompt, enforced by hope | Rules are code. Violations are detected, fed back for revision, and escalated to a human if unfixable. |
| Nobody can explain what the automation did | Linear chains keep no reviewable state | Every node writes a trace step. Every outbound write is a reviewable object. |

The architectural answer to all three is the same: **separate proposing from
doing.** Workflows produce `Effect` objects describing intended writes. A
dispatcher executes them, and only after approval.

---

## Architecture

```
                    ┌─────────────────────────────────────────────┐
   FastAPI  ──────▶ │              LangGraph workflows            │
                    │                                             │
                    │  gather → analyse → narrate ⇄ verify        │
                    │                              ↓              │
                    │                          assemble           │
                    │                              ↓              │
                    │                          propose            │
                    │                         ╱        ╲          │
                    │            await_approval        dispatch   │
                    └─────────────┬───────────────────────┬───────┘
                                  │                       │
                        ┌─────────▼────────┐   ┌──────────▼─────────┐
                        │  Effect registry │   │ Connector bundle   │
                        │  (staged writes) │   │ Meta / Harvest /   │
                        └──────────────────┘   │ Slack / Trello     │
                                               └────────────────────┘
```

**Layer rules, enforced by structure not convention:**

| Node | May do | May not do |
|---|---|---|
| `gather` | I/O against connectors | Any logic |
| `analyse` | Deterministic maths | Call an LLM |
| `narrate` | Call an LLM | Compute a number |
| `verify` | Check prose against the maths | Call an LLM, or rewrite anything |
| `propose` | Construct `Effect`s | Execute anything |
| `dispatch` | Mutate external systems | Anything else |

Constructing an `Effect` cannot cause a side effect — there is a test asserting
exactly that (`test_effects_are_inert_until_dispatched`).

---

## Workflow 1 — Weekly client reporting

```
gather → analyse → narrate ⇄ verify → assemble → propose → [approval gate] → dispatch
```

Pulls Meta Ads performance and Harvest time tracking, computes week-on-week
deltas, ranks material findings by severity and revenue impact, has the LLM
write client commentary grounded strictly in those computed figures, checks
that commentary back against the arithmetic, renders a markdown report, and
stages a Slack post plus one Trello action card per high-severity finding.

Sample trace:

```
run 136b01f06555  [client_report]  status=blocked_on_approval
   1. gather              0.1ms  3 campaigns, 5 time entries
   2. analyse             0.1ms  4 findings (2 high severity)
   3. narrate             0.0ms  narrative via offline (865 chars)
   4. verify              0.0ms  commentary signs agree with the computed figures
   5. assemble            0.0ms  report assembled (45 lines)
   6. propose             0.0ms  3 effects staged, none executed
   7. await_approval      0.0ms  paused - 3 effects awaiting human approval
```

**Materiality thresholds** keep the report readable: movements under 10%, or on
campaigns spending under AED 2,000, are treated as noise and never surface. An
agency automation that flags everything gets ignored within a fortnight.

### The sign check

`verify` exists because a prompt cannot make a guarantee. The model is handed
every figure pre-computed, with the direction and the judgement in separate
columns, and told to reproduce the signs exactly. It still occasionally printed
one inverted — a rising cost is bad news, and a minus sign is how prose usually
signals bad news:

> CPA up to AED 70 **(‑16%)** — while the table two lines above says **+16%**

A client who catches the prose and the table disagreeing stops trusting both.
So the claim is enforced rather than requested. `verify` compares what the
commentary printed against what `compare()` computed, a disagreement earns one
retry with the specific violation attached, and copy that still contradicts the
figures is replaced by the offline engine's templated prose. The trace records
that it happened and why.

It checks characters, not verbs: an explicit sign in front of a magnitude,
across every dash variant a model might reach for — the observed failure used
U+2011, a non-breaking hyphen an ASCII check would have missed. Where two
metrics share a magnitude in opposite directions the figure is skipped rather
than guessed at. **A gate that cries wolf gets switched off, and then it
protects nothing.**

---

## Workflow 2 — Creative production pipeline

```
load_guidelines → generate → score ⇄ revise → select → propose → [gate] → dispatch
```

The interesting part is the bounded revision loop. Generated copy is scored
against machine-checkable brand rules — banned phrases, character limits,
required call-to-action, and a coherence heuristic. Failing variants go back to
the model **with their specific violations attached**, not a vague "try again",
and are re-scored. After `MAX_REVISION_ROUNDS` the loop exits and unfixable
copy is escalated to a human rather than retried forever.

```
   3. score              2/6 passed brand check
   4. revise_round_1     revised 4 failing variants
   5. score              5/6 passed brand check
   6. revise_round_2     revised 1 failing variants
   7. score              5/6 passed brand check
   8. select             5 approved, 1 rejected after 2 revision round(s)
```

The coherence check exists because of a real failure observed while building
this: a naive find-and-replace fix turned *"cheap, fast and the best price
ever"* into *"fast and the"* — which passes a banned-phrase check and a
character count while reading as nonsense. Automated repair needs an automated
sanity check behind it.

---

## The approval gate

`REQUIRE_HUMAN_APPROVAL=true` (the default) makes every workflow halt after
staging its writes. The run persists; the effects are listed; a human releases
them — all of them, or a subset.

```bash
curl -X POST localhost:8000/workflows/client-report -d '{"client":"nova-retail"}'
# → 3 effects, all status "proposed", zero API calls made

# Release the client report but hold the internal action cards
curl -X POST localhost:8000/runs/$RUN_ID/decision \
     -d '{"decision":"approve","effect_indexes":[0]}'
```

Irreversible effects (a Slack post to a client channel cannot be unsent) are
flagged as such in the review list.

---

## The review console

The gate is only as good as the interface in front of it, and the people using
it are account leads, not engineers. So the same FastAPI process serves a
console at `/console/`:

| View | What it is for |
|---|---|
| Overview | What mode this instance is in, and how many writes are staged |
| Run a workflow | Trigger either workflow with validated inputs |
| Approvals | The queue — per-effect checkboxes, irreversible writes flagged, release all or part of a run |
| Run history | Full node-by-node trace, the rendered report, the generated copy with its brand scores |

Plain HTML, CSS and JavaScript — no npm, no bundler, no CDN. That is a
deliberate constraint rather than a shortcut: the console runs on the same terms
as the rest of the project (no credentials, no network, one process), and an
agency's internal team can read and edit it without a toolchain.

It is transport, like the API layer. No logic lives in the browser: every figure
it displays was computed in `analysis.py`, and every status it shows comes from
the server. A refresh re-reads `/runs`, so the console can never disagree with
the orchestrator about what was sent.

**Accessibility** is treated as part of the deliverable, not a polish pass:
semantic landmarks and a skip link; a heading order that survives an audit;
labelled controls with an error summary that links to the offending field;
one polite live region for status rather than toasts that vanish before they can
be read; focus moved to `<main>` on navigation; a native `<dialog>` for the
irreversible-send confirmation, so focus trapping and Escape come from the
platform; status carried by text, never by colour alone; light and dark themes
both clearing WCAG AA contrast; and `prefers-reduced-motion` honoured.

---

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # optional — defaults run fully offline

python scripts/demo.py        # both workflows, narrated
python -m pytest -q           # 101 tests
uvicorn agencyops.api.main:app --reload --app-dir src
```

Then open http://localhost:8000 for the console, or
http://localhost:8000/docs for the API.

### Three run modes, one build

| Mode | `CONNECTOR_MODE` | `LLM_PROVIDER` | Use |
|---|---|---|---|
| Offline demo | `mock` | `auto` (no key) | Demos, CI, this repo's default |
| Staging | `mock` | `ollama` | Tune prompts against real generation, zero client risk |
| Production | `live` | `ollama` | Real Meta / Harvest / Slack / Trello |

### Choosing a model

Generation runs through Ollama by default, on `gpt-oss:120b-cloud`:

```bash
ollama signin                        # cloud models only
ollama pull gpt-oss:120b-cloud
echo "LLM_PROVIDER=ollama" >> .env
```

Swap `OLLAMA_MODEL` for a tag you have pulled locally (`gpt-oss:20b`) to run
with no external service at all, or set `LLM_PROVIDER=gemini` with a
`GEMINI_API_KEY` — both satisfy the same `LLMEngine` protocol and no graph code
changes.

`auto` deliberately never resolves to Ollama. A daemon happening to run on the
box is not consent to send client data through it, so pointing the project at a
provider is always an explicit line in `.env`. The test suite pins itself to
`offline` at conftest import, so a configured checkout still has a
deterministic, network-free suite.

The offline LLM engine is not a stub returning filler. It produces genuine,
data-grounded output for every task the graphs need — which is why the demo
runs anywhere, the test suite is deterministic, and a provider outage degrades
reporting to templated prose instead of taking it down. That last part is
enforced rather than asserted: every remote engine catches its own failures per
call and falls through to the offline engine, and the trace records which
engine actually produced the text, so a week of templated commentary is
diagnosable rather than mysterious.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Active connector mode, LLM engine, gate status |
| `GET` | `/clients` | Client roster, and which can run the creative pipeline |
| `POST` | `/workflows/client-report` | Run reporting; returns staged effects |
| `POST` | `/workflows/creative` | Run creative pipeline |
| `GET` | `/runs` · `/runs/{id}` | Run list and full trace |
| `GET` | `/runs/{id}/report` | Rendered markdown report |
| `POST` | `/runs/{id}/decision` | Approve or reject staged effects |
| `GET` | `/console/` | The review console |

---

## Layout

```
src/agencyops/
  config.py          environment-driven settings
  llm.py             Ollama + Gemini engines, deterministic offline fallback
  analysis.py        deterministic metrics, deltas, signal detection
  observability.py   run tracing, persisted as plain JSON
  runstore.py        paused-run registry and approval dispatcher
  connectors/
    base.py          protocols, dataclasses, the Effect model
    mock.py          fixture-backed connectors
    live.py          Meta Ads, Harvest, Slack, Trello REST clients
  graphs/
    state.py         typed graph state
    client_report.py reporting workflow
    creative_pipeline.py  creative workflow with the revision loop
  api/               FastAPI transport layer
    static/          the review console — hand-written HTML, CSS, JS
tests/               101 tests
scripts/demo.py      narrated end-to-end run
```

---

## What is deliberately not here

Honest scope boundaries for a prototype:

- **No durable checkpointer.** Paused runs live in memory. LangGraph's
  Postgres checkpointer drops into `build_*_graph()` without touching node
  code — the graphs are already written as pure state transitions.
- **No auth on the API or the console.** Both assume a trusted network.
  Production needs workspace-scoped tokens and a signed-in reviewer, since the
  approval gate is a security boundary and "who released this?" is an audit
  question the trace cannot currently answer.
- **The console has no realtime channel.** Workflows run synchronously and the
  console re-reads `/runs` after every action. Two reviewers working the queue
  at once would not see each other's decisions until they refreshed.
- **Live connectors are wired but unexercised.** They implement the same
  protocols and are covered by the type contracts, not by integration tests
  against real accounts.
- **Scoring rules are heuristic, not a trained classifier.** For brand
  compliance that is the right call — an agency needs to read, edit and trust
  the rule set, which means it has to stay legible.
