"""Text a person reads: the plan card, the copy card for other chats, and a calendar invite."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .negotiation import ACTIVITIES, PlanResult
from .timeutil import DAYS, fmt_clock


def when_line(start: datetime, end: datetime) -> str:
    return f"{DAYS[start.weekday()]}, {start:%b} {start.day} · {fmt_clock(start)}–{fmt_clock(end)}"


def copy_card(r: PlanResult) -> str:
    """Plain text anyone can paste into iMessage, GroupMe or WhatsApp."""
    p = r.plan
    act = ACTIVITIES[p.kind]
    lines = [f"{act.emoji} {p.kind.title()} plan",
             f"📅 {when_line(p.start, p.end)}"]
    if p.venue:
        lines.append(f"📍 {p.venue.name}" + (f" ({p.venue.area})" if p.venue.area else ""))
        lines.append(f"💸 about ${p.venue.price_pp:.0f} each" if p.venue.price_pp else "💸 free")
    lines.append(f"🙋 {', '.join(r.confirmed)}" + (f" · maybe: {', '.join(r.maybe)}" if r.maybe else ""))
    lines.append("Reply 'in' to join. Planned with Free Tonight")
    return "\n".join(lines)


def _ics_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_invite(r: PlanResult) -> str:
    p = r.plan
    where = p.venue.name if p.venue else "TBD"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Free Tonight//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@freetonight",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_ics_time(p.start)}",
        f"DTEND:{_ics_time(p.end)}",
        f"SUMMARY:{_ics_escape(p.kind.title() + ' with ' + ', '.join(r.confirmed + r.maybe))}",
        f"LOCATION:{_ics_escape(where)}",
        f"DESCRIPTION:{_ics_escape('Planned with Free Tonight. Going: ' + ', '.join(r.confirmed))}",
        "BEGIN:VALARM", "TRIGGER:-PT60M", "ACTION:DISPLAY", "DESCRIPTION:Plans in 1 hour", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"
