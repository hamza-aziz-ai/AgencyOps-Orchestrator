# Architecture notes

## Why a graph

The requirement that forces graph structure is the approval gate. A workflow
must be able to reach a state where the work is complete, the outbound writes
are staged, and execution is suspended pending a human decision — then resume
from that exact state.

Linear automation (Zapier, Make, a shell script) has two terminal states:
completed or failed. There is no "done but not sent". LangGraph gives typed
state, conditional edges and a resumable execution model, which is the minimum
needed to express that.

The creative pipeline additionally needs a cycle (`score → revise → score`)
with a bounded exit condition. That is a graph, not a pipeline.

## State design

Both graphs use `TypedDict` state with reducer-annotated accumulator fields:

```python
findings: Annotated[list[dict], _append]
effects:  Annotated[list[Effect], _append]
errors:   Annotated[list[str], _append]
```

Nodes return partial state updates rather than mutating. This keeps nodes pure
with respect to graph state, which is what makes swapping in a durable
checkpointer a drop-in change later.

## Error handling

Nodes append to `errors` and routing functions check it. A failed `gather`
short-circuits to `END` before `propose` ever constructs an `Effect`, so a
failed run cannot leave staged writes behind. `test_unknown_client_short_
circuits_without_side_effects` asserts this.

Dispatch failures are caught per-effect: one failing Trello call marks that
effect `failed` and continues, rather than aborting a batch halfway with no
record of what got through.

## Why analysis is not in the graph

`analysis.py` is a pure module with no graph or LLM dependency. It is the part
most likely to be wrong in a way that matters commercially, so it is the part
most heavily unit-tested — twelve tests covering thresholds, zero-division,
severity ranking and delta computation. Keeping it out of the graph means those
tests run without constructing any graph state.

## Engine selection and degradation

```
graphs ──▶ LLMEngine (Protocol) ──▶ OllamaEngine | GeminiEngine | OfflineEngine
                                          └── failure ──▶ OfflineEngine
```

Same shape as the connector boundary, for the same reason: graph code never
branches on which engine is active, so swapping gpt-oss for Gemini is a line in
`.env`.

Two decisions worth defending.

**Degradation is per call, not per process.** `RemoteEngine.complete()` catches,
logs, and falls through to the offline engine; subclasses implement `_generate`
and nothing else. Handling this only at construction time — the obvious
placement — would mean a provider that is reachable at startup and dies at
09:03 on Monday takes weekly reporting down with it. A node raising on an
expected failure would also strand the run with no report and no trace of why,
which is the outcome the whole design exists to prevent.

The `LLMResponse` reports the engine that *produced* the text, not the one that
was configured, and `narrate` records it in the trace. "Why does this week's
commentary read like a template" is then a question the run answers itself.

**`auto` never resolves to a provider the user did not name.** An Ollama daemon
listening on localhost is not consent to send client performance data through
it, and inferring otherwise would make the safe path the one you have to know
to ask for. Tests go further and pin `LLM_PROVIDER=offline` in the environment
at conftest import, so a checkout configured for Ollama still runs a
deterministic, network-free suite.

## Connector protocol boundary

```
graphs ──▶ AdsConnector (Protocol) ──▶ MockMetaAds | MetaAdsConnector
```

Graphs never import from `connectors.live` or `connectors.mock`. They receive a
`ConnectorBundle` at build time. Consequences:

- Tests inject mocks with no patching or monkeypatching anywhere in the suite
- Live/mock selection is one branch in `build_bundle()`
- Adding WhatsApp means adding a `WriteConnector` subclass; no graph changes

## The console is a client, not a layer

```
browser ──▶ /health /clients /workflows/* /runs/* ──▶ same FastAPI process
```

The review console holds no state the server does not already own. It renders
figures computed in `analysis.py`, statuses read from `StoredRun.summary()`, and
documents produced by the `assemble` node — it derives nothing. Two consequences
worth the constraint:

- A refresh cannot produce a different answer than the API would. There is no
  cache to go stale and no client-side copy of the truth to drift.
- Everything the console can do, `curl` can do. The UI is a convenience over
  the approval gate, never a second path through it.

This is why `/runs/{id}` returns artifacts rather than only effects: a reviewer
opening a run cold needs the report and the generated copy, and the alternative
— replaying the workflow to rebuild them — would mean re-running an LLM to
render a page.

No build step, deliberately. An agency inherits this codebase; a console its
team can open in an editor and change is worth more than one that needs a
toolchain resurrected first.

## The dispatcher as a chokepoint

Exactly one node per graph is permitted to cause side effects, and it only
processes effects in `proposed` or `approved` status. Concentrating mutation in
one place means the audit question — "what did this system actually do?" — has
one place to look, and the safety property (nothing sends without approval) has
one place to be enforced and one test to prove it.
