"""Vision path: read a circuit-schematic photo with the multimodal main LLM.

The main model (Qwen3.6-35B-A3B) is image-text-to-text, so a photo of a
schematic can be transcribed into a SPICE netlist and handed to the normal
evaluate_circuit flow (simulation + charts). An image that is not a readable
schematic falls back to a plain vision-chat answer instead.

Images travel as (base64, mime) pairs and are inlined as data-URI
`image_url` content parts (OpenAI vision format, supported by vLLM).
"""

import logging
import re

from . import config, llm
from .flows.evaluate_circuit import lang_directive

log = logging.getLogger("vision")

# A complete top-level deck must end with a bare `.end` line (`.ends` only
# closes a subcircuit — a fragment with just `.ends` is not simulatable).
_DOT_END = re.compile(r"(?im)^\s*\.end\s*$")


def _image_parts(images: list[tuple[str, str]]) -> list[dict]:
    return [{"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{b64}"}}
            for b64, mime in images]


EXTRACT_SYSTEM = """\
You read electronic circuit schematics from images and transcribe them into
ngspice-compatible SPICE netlists.

If the image contains a readable circuit schematic:
- Set `is_circuit` to true and put the COMPLETE netlist in `netlist`.
- Transcribe faithfully: every component with the designator and value shown.
  Use a reasonable default ONLY for a value the image does not show, and list
  every such guess in `notes`.
- Start the netlist with a title comment line (`* <circuit name>`) — ngspice
  treats the first line as a title, so a component on line 1 would be lost.
- Ground is node 0. Name the main output node `out` — the simulator's metrics
  expect that exact name.
- Include the input source if one is drawn (an AC source for
  amplifiers/filters), exactly ONE analysis directive that suits the circuit
  (.ac for amplifiers/filters, .tran for oscillators/switching, .op
  otherwise), and finish with `.end`.

If the image is NOT a circuit schematic, or is too blurry/incomplete to
transcribe reliably, set `is_circuit` to false, leave `netlist` empty and
explain why in `notes`.
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_circuit": {"type": "boolean"},
        "netlist": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["is_circuit", "netlist"],
}


async def netlist_from_image(message: str,
                             images: list[tuple[str, str]]) -> dict:
    """Try to transcribe a schematic photo into a netlist.

    Returns {"netlist": str, "notes": str}; empty netlist means the image is
    not a (readable) schematic and the caller should fall back to vision chat.
    """
    content = ([{"type": "text",
                 "text": f"User request: {message or '(no caption)'}"}]
               + _image_parts(images))
    # The multimodal chat template rejects a system message that is not the
    # first message, so the language directive rides in the single system turn.
    # Generous max_tokens: the thinking model can burn thousands of reasoning
    # tokens on a messy photo before emitting the JSON. If it still returns
    # nothing parseable, degrade to vision chat instead of failing the request.
    try:
        out = await llm.complete_json(
            [{"role": "system",
              "content": EXTRACT_SYSTEM + "\nWrite `notes` in the user's "
                         "language. " + lang_directive(message)},
             {"role": "user", "content": content}],
            EXTRACT_SCHEMA, max_tokens=16384)
    except Exception:
        log.warning("netlist extraction unparseable — vision-chat fallback",
                    exc_info=True)
        return {"netlist": "", "notes": ""}
    netlist = (out.get("netlist") or "").strip()
    notes = (out.get("notes") or "").strip()
    # Sanity gate: a transcription without a terminating .end line is a
    # truncated/hallucinated netlist — do not feed it to the simulator.
    if not out.get("is_circuit") or not _DOT_END.search(netlist):
        netlist = ""
    return {"netlist": netlist, "notes": notes}


async def chat(message: str, images: list[tuple[str, str]],
               history: list | None = None) -> str:
    """Plain vision chat about the image(s) — the non-schematic fallback."""
    content = [{"type": "text", "text": message}] + _image_parts(images)
    messages = ([{"role": "system",
                  "content": config.ASSISTANT_IDENTITY
                             + lang_directive(message)}]
                + (history or [])
                + [{"role": "user", "content": content}])
    return await llm.complete(messages, temperature=0.6)
