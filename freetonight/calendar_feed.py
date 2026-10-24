"""Read busy times from a private calendar link (Google, Outlook or iCloud "secret iCal address").

Only start and end times are read. Event names, places and notes are skipped and never stored, so the agent
knows when someone is busy but not why. Standard library only.
"""
from __future__ import annotations

import re
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from .profile import Commitment

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Only fetch from calendar providers, so a pasted link can't make the bot fetch arbitrary or internal URLs.
ALLOWED_HOSTS = ("calendar.google.com", "outlook.office365.com", "outlook.live.com")
ALLOWED_SUFFIXES = (".icloud.com",)
MAX_BYTES = 5_000_000
TIMEOUT = 10

HELP = ("Google Calendar on a computer → ⚙️ Settings → click your calendar under *Settings for my calendars* → "
        "*Integrate calendar* → copy **Secret address in iCal format**. "
        "(Outlook: Settings → Calendar → Shared calendars → Publish. iPhone: share the calendar as a public link.)")

DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _allowed(host: str | None) -> bool:
    host = (host or "").lower()
    return host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)


def check_url(url: str) -> str:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    u = urlparse(url)
    if u.scheme != "https" or not _allowed(u.hostname):
        raise ValueError("That doesn't look like a calendar link. Paste the secret iCal address from Google "
                         "Calendar (it starts with https://calendar.google.com/calendar/ical/).")
    return url


def fetch(url: str) -> str:
    url = check_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "FreeTonight/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 (host checked above)
            if not _allowed(urlparse(r.geturl()).hostname):
                raise ValueError("The calendar link redirected somewhere unexpected.")
            data = r.read(MAX_BYTES + 1)
    except OSError as exc:
        raise ValueError("Couldn't open that calendar link. Check it's the *secret* iCal address.") from exc
    if len(data) > MAX_BYTES:
        raise ValueError("That calendar is too big to read.")
    text = data.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("That link didn't return a calendar. Use the secret address in iCal format.")
    return text


def busy_from_link(url: str, dates: list[date]) -> list[Commitment]:
    return busy_from_ics(fetch(url), dates)


# ---------------------------------------------------------------- parsing

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _prop(line: str) -> tuple[str, dict[str, str], str]:
    head, _, value = line.partition(":")
    name, *params = head.split(";")
    return name.upper(), {k.upper(): v for k, _, v in (p.partition("=") for p in params)}, value.strip()


def _to_local(value: str, params: dict[str, str]) -> datetime | None:
    """Parse a DTSTART/DTEND value into the bot machine's local time. None for all-day values."""
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return None
    m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z?)", value)
    if not m:
        return None
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    if m.group(4) == "Z":
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    tzid = params.get("TZID", "").strip('"')
    if tzid and ZoneInfo:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzid)).astimezone().replace(tzinfo=None)
        except Exception:  # unknown zone name: treat as local
            pass
    return dt


def _events(text: str) -> list[dict]:
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": set()}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, params, value = _prop(line)
            if name in ("DTSTART", "DTEND"):
                cur[name] = _to_local(value, params)
                cur[name + "_allday"] = cur[name] is None
            elif name == "DURATION":
                m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", value)
                if m:
                    d, h, mi = (int(x or 0) for x in m.groups())
                    cur["DURATION"] = timedelta(days=d, hours=h, minutes=mi)
            elif name == "RRULE":
                cur["RRULE"] = dict(p.partition("=")[::2] for p in value.split(";"))
            elif name == "EXDATE":
                for v in value.split(","):
                    dt = _to_local(v, params)
                    if dt:
                        cur["exdates"].add(dt)
            elif name in ("TRANSP", "STATUS"):
                cur[name] = value.upper()
    return events


def _occurrences(ev: dict, first: date, last: date) -> list[datetime]:
    start: datetime = ev["DTSTART"]
    rule = ev.get("RRULE")
    if not rule:
        return [start] if first <= start.date() <= last else []
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        u = _to_local(rule["UNTIL"] if "T" in rule["UNTIL"] else rule["UNTIL"] + "T235900", {})
        until = u
    byday = [DAY_CODES.index(d[-2:]) for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAY_CODES]
    out, n = [], 0
    if freq == "WEEKLY":
        days = byday or [start.weekday()]
        week = start.date() - timedelta(days=start.weekday())
        while week <= last and (count is None or n < count):
            for wd in sorted(days):
                d = week + timedelta(days=wd)
                occ = datetime.combine(d, start.time())
                if occ < start:
                    continue
                if (until and occ > until) or (count is not None and n >= count):
                    return out
                n += 1
                if first <= d <= last:
                    out.append(occ)
            week += timedelta(weeks=interval)
    elif freq == "DAILY":
        occ = start
        while occ.date() <= last and (count is None or n < count):
            if until and occ > until:
                break
            n += 1
            if occ.date() >= first and (not byday or occ.weekday() in byday):
                out.append(occ)
            occ += timedelta(days=interval)
    else:  # monthly/yearly repeats rarely matter for this week's plans; count the first one only
        if first <= start.date() <= last:
            out.append(start)
    return out


def busy_from_ics(text: str, dates: list[date]) -> list[Commitment]:
    """Busy blocks on the given dates as one-off commitments, with no names attached."""
    if not dates:
        return []
    first, last = min(dates), max(dates)
    wanted = set(dates)
    out: list[Commitment] = []
    for ev in _events(text):
        start = ev.get("DTSTART")
        if start is None or ev.get("TRANSP") == "TRANSPARENT" or ev.get("STATUS") == "CANCELLED":
            continue  # all-day, marked "free", or cancelled
        length = (ev["DTEND"] - start) if ev.get("DTEND") else ev.get("DURATION", timedelta(hours=1))
        if length <= timedelta(0) or length > timedelta(hours=16):
            continue
        for occ in _occurrences(ev, first - timedelta(days=1), last):
            if occ in ev["exdates"]:
                continue
            end = occ + length
            day = occ.date()
            while day <= end.date():
                if day in wanted:
                    s = max(occ, datetime.combine(day, time(0)))
                    e = min(end, datetime.combine(day, time(23, 59)))
                    if e > s:
                        out.append(Commitment(days=[day.weekday()], start=s.strftime("%H:%M"),
                                              end=e.strftime("%H:%M"), label="calendar", on_date=day.isoformat()))
                day += timedelta(days=1)
    return out
