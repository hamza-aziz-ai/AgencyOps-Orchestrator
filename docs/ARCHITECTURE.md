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

## Connector protocol boundary

```
graphs ──▶ AdsConnector (Protocol) ──▶ MockMetaAds | MetaAdsConnector
```

Graphs never import from `connectors.live` or `connectors.mock`. They receive a
`ConnectorBundle` at build time. Consequences:

- Tests inject mocks with no patching or monkeypatching anywhere in the suite
- Live/mock selection is one branch in `build_bundle()`
- Adding WhatsApp means adding a `WriteConnector` subclass; no graph changes

## The dispatcher as a chokepoint

Exactly one node per graph is permitted to cause side effects, and it only
processes effects in `proposed` or `approved` status. Concentrating mutation in
one place means the audit question — "what did this system actually do?" — has
one place to look, and the safety property (nothing sends without approval) has
one place to be enforced and one test to prove it.
