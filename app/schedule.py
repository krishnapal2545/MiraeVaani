"""When a campaign is allowed to dial.

Two rules stack, and only one of them belongs to the customer. The campaign's
own window is theirs — "10:00 to 19:00, Monday to Saturday, Asia/Kolkata". The
outer bound is not: Indian telecom regulation restricts the hours commercial
calls may be placed, so a window reaching outside 09:00–21:00 is clamped rather
than honoured. A setting in the UI must be able to narrow calling hours and
must not be able to widen them.

Times are evaluated in the campaign's own timezone, never the server's. A
laptop demoed from a different timezone, or a server in another region, must
still call people during *their* daytime.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

# TRAI restricts commercial calling hours. A campaign may narrow this window;
# nothing may widen it. This is a coarse stand-in for real compliance — actual
# DND/DLT registry checks are a separate piece of work.
LEGAL_START = time(9, 0)
LEGAL_END = time(21, 0)

DEFAULT_TIMEZONE = "Asia/Kolkata"


def _parse_time(value: str, fallback: time) -> time:
    try:
        hours, minutes = str(value).split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return fallback


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def effective_window(window_start: str, window_end: str) -> tuple[time, time]:
    """The campaign's window after the legal bound is applied."""
    start = max(_parse_time(window_start, LEGAL_START), LEGAL_START)
    end = min(_parse_time(window_end, LEGAL_END), LEGAL_END)
    return start, end


def window_open(campaign: dict, now: datetime | None = None) -> bool:
    """Whether this campaign may place a call at this moment."""
    zone = _zone(campaign.get("timezone"))
    local = (now.astimezone(zone) if now else datetime.now(zone))

    # Weekdays as Python numbers them: Monday is 0. An empty list means the
    # customer did not restrict days, not that no day is allowed.
    days = {
        int(day)
        for day in str(campaign.get("days") or "").split(",")
        if day.strip().isdigit()
    }
    if days and local.weekday() not in days:
        return False

    start, end = effective_window(
        campaign.get("window_start", ""), campaign.get("window_end", "")
    )
    if start >= end:
        return False
    return start <= local.time() < end
