"""Optional: let Claude read a messy, typed-out schedule. Without ANTHROPIC_API_KEY the strict parser is used."""
from __future__ import annotations

import json
import logging
import os

from .profile import Commitment, parse_schedule

log = logging.getLogger(__name__)
MODEL = os.environ.get("FREETONIGHT_MODEL", "claude-opus-5-5")

_SCHEMA = {
    "type": "object",
    "properties": {
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "days": {"type": "array", "items": {"type": "integer"}},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "label": {"type": "string"},
                    "flex_minutes": {"type": "integer"},
                },
                "required": ["days", "start", "end", "label", "flex_minutes"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["commitments"],
    "additionalProperties": False,
}

_PROMPT = """Turn this student's description of their weekly schedule into commitments.
days: weekday numbers, 0=Monday through 6=Sunday. start/end: 24-hour "HH:MM".
label: short name of the commitment. flex_minutes: minutes they said they could skip, leave early or arrive late
(0 if they didn't say it's flexible). Leave out anything that isn't a recurring weekly time block.

Schedule:
"""


def parse_schedule_smart(text: str) -> tuple[list[Commitment], str]:
    """Returns (commitments, how). Tries the strict parser, then Claude if a key is set."""
    try:
        return parse_schedule(text), "parser"
    except ValueError as strict_error:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise
        try:
            return _parse_with_claude(text), "claude"
        except Exception as exc:  # network, refusal, bad output: fall back to the strict parser's message
            log.warning("Claude schedule parse failed: %s", exc)
            raise strict_error from None


def _parse_with_claude(text: str) -> list[Commitment]:
    import anthropic

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _PROMPT + text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined")
    raw = next(b.text for b in response.content if b.type == "text")
    out = []
    for c in json.loads(raw)["commitments"]:
        days = sorted({d for d in c["days"] if 0 <= d <= 6})
        if not days:
            continue
        # Round-trip through the strict format so bad clock values fail loudly.
        line = f"{','.join(['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][d] for d in days)} {c['start']}-{c['end']} {c['label']}"
        parsed = parse_schedule(line)[0]
        parsed.flex_minutes = max(0, int(c["flex_minutes"]))
        out.append(parsed)
    return out
