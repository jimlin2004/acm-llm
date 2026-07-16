# System Analysis Documentation Kit

A reusable set of templates for analyzing any system (software, infrastructure, business process, or hardware) in a structured, comprehensive way.

> **This instance is filled in for the ACM LLM Lab stack** (v0.1, 2026-07-09) —
> see [06-evaluation-findings.md](06-evaluation-findings.md) for the conclusions
> and [checklist.md](checklist.md) for what remains open.

## How to use

1. Copy this whole `system-analysis/` folder into the target project (or fill it in place for a one-off analysis).
2. Work through the documents **in order** — each one builds on the previous:

| # | Document | Answers the question |
|---|----------|----------------------|
| 1 | [01-objectives-scope.md](01-objectives-scope.md) | *Why does this system exist, and where are its limits?* |
| 2 | [02-components-architecture.md](02-components-architecture.md) | *What is it made of, and how are the parts arranged?* |
| 3 | [03-data-flow-processes.md](03-data-flow-processes.md) | *What goes in, what happens inside, what comes out, what is stored?* |
| 4 | [04-boundaries-interfaces.md](04-boundaries-interfaces.md) | *How does it talk to users and to other systems?* |
| 5 | [05-non-functional-requirements.md](05-non-functional-requirements.md) | *Does it run fast, securely, reliably, and within constraints?* |
| 6 | [06-evaluation-findings.md](06-evaluation-findings.md) | *Where are the bottlenecks, risks, and improvement opportunities?* |

3. Use [checklist.md](checklist.md) to verify nothing was skipped before you sign off.

## Recommended analysis process (top-down)

**Phase 1 — Black box.** Treat the entire system as an opaque box. Only document:
Input → (system) → Output. Fill in the black-box section of doc 03 first; do not
open the internals yet. This forces agreement on *what the system does* before
debating *how it does it*.

**Phase 2 — White box.** Open the box. Decompose the system into modules /
subsystems (doc 02), then trace data through them with Data Flow Diagrams or
sequence diagrams (doc 03). Map every external touchpoint (doc 04).

**Phase 3 — Evaluate & optimize.** With the full picture, hunt for:
- **Bottlenecks** — the slowest or most contended stage in any flow.
- **Single Points of Failure (SPOF)** — any component whose failure takes the system down.
- **Gaps** — missing security controls, missing backups, undocumented ownership.

Record everything in doc 06 with a severity and a proposed remediation.

## Conventions

- Mark unknowns explicitly as `TBD` — an honest gap beats a guessed answer.
- Prefer diagrams (Mermaid works in most Markdown renderers) over prose for structure and flows.
- Every external dependency must have a named owner/contact.
- Date and version each document; analysis of a moving system goes stale fast.
