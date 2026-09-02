"""Flow registry — the extension point of the orchestrator.

A new business flow = one module in app/flows/ that builds a LangGraph
StateGraph and calls register(FlowSpec(...)). Nothing else to touch:
the router, engine and API pick it up automatically.
"""

from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional


@dataclass
class Attachment:
    name: str
    content: str


class MissingParams(Exception):
    """Raised by a flow's prepare() when required inputs are absent.

    The message is sent back to the user as a clarify response instead of
    running the flow with guessed inputs.

    `param`, when set, names the single params_schema key that's missing.
    app/main.py then remembers a resumable clarify for it: the user's very
    next reply is taken as that param's value and the original message +
    attachments are replayed against the same flow — the same "answer once,
    the request continues" UX as the multi-candidate clarify. Leave it None
    when no short answer can fix things (e.g. "no netlist found" — the user
    has to resend a whole file, there's nothing to slot a one-word reply
    into).
    """

    def __init__(self, message: str, param: str | None = None):
        super().__init__(message)
        self.param = param


@dataclass
class FlowSpec:
    flow_id: str
    # Natural-language trigger description — goes into the router prompt.
    description: str
    # JSON schema of params the router extracts from the user message.
    # Large payloads (file contents) must NOT go through the router; they
    # are delivered via attachments and bound in prepare().
    params_schema: dict
    # () -> uncompiled StateGraph. Compiled once at startup with the
    # shared checkpointer, so interrupt()/resume works out of the box.
    build: Callable[[], Any]
    # (message, attachments, params) -> initial graph state.
    # Raise MissingParams to ask the user for what's missing.
    prepare: Callable[[str, list[Attachment], dict], dict]
    # Optional streaming runner: (initial_state) -> async iterator of events
    # {"type": "status"|"delta", "text": str}. When present, POST /flow/stream
    # uses it to push the answer token-by-token instead of polling. Flows that
    # need human-in-the-loop interrupts should leave this None (use the engine).
    stream_run: Optional[Callable[[dict], AsyncIterator[dict]]] = None


FLOWS: dict[str, FlowSpec] = {}


def register(spec: FlowSpec) -> None:
    if spec.flow_id in FLOWS:
        raise ValueError(f"duplicate flow_id: {spec.flow_id}")
    FLOWS[spec.flow_id] = spec
