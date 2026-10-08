#!/usr/bin/env python3
"""Filter an Outlook/Exchange published ICS feed down to confirmed events.

Outlook's METHOD:PUBLISH feeds strip ATTENDEE/ORGANIZER, so there is no
PARTSTAT to read. Response status has to be inferred from the subject prefix
and the X-MICROSOFT-CDO-* properties instead.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict, defaultdict

CRLF = "\r\n"

# Outlook localises the subject prefix it stamps onto cancelled and
# followed meetings, so match the known translations.
CANCELLED_PREFIX = re.compile(
    r"^\s*(cancell?ed|canceled|atšaukta[s]?|atsaukta[s]?|annulliert|absage|"
    r"annulé[e]?|annullato|cancelado|anulowane|отменено|отменена|avlyst|"
    r"geannuleerd|inställt|peruttu)\s*:",
    re.IGNORECASE,
)

FOLLOWING_PREFIX = re.compile(
    r"^\s*(stebima|following|folge ich|suivi|siguiendo|seguito|volgen|følger|"
    r"seuraan|отслеживается)\s*:",
    re.IGNORECASE,
)


def split_prop(line: str):
    """Split an unfolded content line into (NAME, params, value)."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            left, value = line[:i], line[i + 1 :]
            name, _, params = left.partition(";")
            return name.strip().upper(), params, value
    return line.strip().upper(), "", ""


def unfold(text: str) -> list[str]:
    text = text.lstrip("\ufeff")
    text = re.sub(r"\r?\n[ \t]", "", text)
    return [l.rstrip("\r") for l in text.split("\n")]


def fold(line: str) -> str:
    """Fold a content line to 75 octets without splitting a UTF-8 character."""
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks, cur, cur_len, limit = [], [], 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if cur_len + n > limit:
            chunks.append("".join(cur))
            cur, cur_len, limit = [ch], n, 74
        else:
            cur.append(ch)
            cur_len += n
    chunks.append("".join(cur))
    return CRLF.join([chunks[0]] + [" " + c for c in chunks[1:]])


class Event:
    __slots__ = ("lines", "props", "uid", "recurrence_id", "recurrence_params")

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.props: dict[str, str] = {}
        self.recurrence_id = None
        self.recurrence_params = ""
        for line in lines:
            name, params, value = split_prop(line)
            self.props.setdefault(name, value)
            if name == "RECURRENCE-ID":
                self.recurrence_id = value
                # RANGE=THISANDFUTURE is meaningless on an EXDATE.
                self.recurrence_params = ";".join(
                    p for p in params.split(";") if p and not p.upper().startswith("RANGE=")
                )
        self.uid = self.props.get("UID", "")

    def get(self, name: str, default: str = "") -> str:
        return self.props.get(name, default)

    @property
    def is_master(self) -> bool:
        return self.recurrence_id is None

    @property
    def has_rrule(self) -> bool:
        return "RRULE" in self.props


def classify(ev: Event, cfg) -> str | None:
    """Return a drop reason, or None to keep the event."""
    summary = ev.get("SUMMARY")
    status = ev.get("STATUS").upper()
    busy = ev.get("X-MICROSOFT-CDO-BUSYSTATUS").upper()
    intended = ev.get("X-MICROSOFT-CDO-INTENDEDSTATUS").upper()
    allday = ev.get("X-MICROSOFT-CDO-ALLDAYEVENT").upper() == "TRUE"

    if cfg.drop_cancelled and (status == "CANCELLED" or CANCELLED_PREFIX.match(summary)):
        return "cancelled"

    if cfg.drop_following and FOLLOWING_PREFIX.match(summary):
        return "following"

    # Out-of-office / holidays are free-ish but are genuinely the user's own
    # entries, so never treat them as a declined meeting.
    if busy == "OOF" or (allday and cfg.keep_allday):
        return None

    # A declined or followed meeting keeps the organiser's intent (BUSY) while
    # the user's own copy drops to FREE. That mismatch is the signature.
    if cfg.drop_declined and busy == "FREE" and intended in ("BUSY", "TENTATIVE", "OOF"):
        return "declined-or-following"

    if cfg.drop_tentative and busy == "TENTATIVE":
        return "tentative"

    if cfg.drop_free and busy == "FREE":
        return "free"

    return None


def filter_calendar(text: str, cfg):
    lines = unfold(text)

    header: list[str] = []
    footer: list[str] = []
    events: list[Event] = []
    cur: list[str] | None = None
    depth_other = 0
    seen_event = False

    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = []
            seen_event = True
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(Event(cur))
            cur = None
            continue
        if cur is not None:
            cur.append(line)
            continue
        if line == "END:VCALENDAR":
            footer.append(line)
            continue
        if not line.strip():
            continue
        # Nested components (VTIMEZONE) belong to the header.
        if line.startswith("BEGIN:"):
            depth_other += 1
        elif line.startswith("END:"):
            depth_other -= 1
        if not seen_event or depth_other > 0 or line.startswith(("BEGIN:", "END:")):
            header.append(line)
        else:
            footer.insert(0, line)

    by_uid: "OrderedDict[str, list[Event]]" = OrderedDict()
    for ev in events:
        by_uid.setdefault(ev.uid, []).append(ev)

    stats = defaultdict(int)
    kept: list[Event] = []
    exdates: dict[int, list[tuple[str, str]]] = defaultdict(list)

    for uid, group in by_uid.items():
        master = next((e for e in group if e.is_master), None)
        master_reason = classify(master, cfg) if master is not None else None

        if master is not None and master_reason:
            # Dropping the series drops every exception with it.
            stats[master_reason] += len(group)
            continue

        for ev in group:
            if ev is master:
                kept.append(ev)
                continue
            reason = classify(ev, cfg)
            if not reason:
                kept.append(ev)
                continue
            stats[reason] += 1
            # The master's RRULE would regenerate this slot, so suppress it.
            if master is not None and master.has_rrule and ev.recurrence_id:
                exdates[id(master)].append((ev.recurrence_params, ev.recurrence_id))

    out: list[str] = []
    for line in header:
        out.append(line)

    for ev in kept:
        out.append("BEGIN:VEVENT")
        extra = exdates.get(id(ev))
        if extra:
            grouped: "OrderedDict[str, list[str]]" = OrderedDict()
            for params, value in extra:
                grouped.setdefault(params, []).append(value)
            emitted = False
            for line in ev.lines:
                name, _, _ = split_prop(line)
                out.append(line)
                # Keep EXDATE next to the rule it constrains.
                if name == "RRULE" and not emitted:
                    emitted = True
                    for params, values in grouped.items():
                        prefix = f"EXDATE;{params}" if params else "EXDATE"
                        out.append(f"{prefix}:{','.join(values)}")
            if not emitted:
                for params, values in grouped.items():
                    prefix = f"EXDATE;{params}" if params else "EXDATE"
                    out.append(f"{prefix}:{','.join(values)}")
        else:
            out.extend(ev.lines)
        out.append("END:VEVENT")

    out.extend(footer or ["END:VCALENDAR"])

    body = CRLF.join(fold(l) for l in out) + CRLF
    stats["kept"] = len(kept)
    stats["total"] = len(events)
    stats["exdates_added"] = sum(len(v) for v in exdates.values())
    return body, stats


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--url", help="ICS feed URL (defaults to $ICS_URL)")
    src.add_argument("--input", help="Read the feed from a local file instead")
    p.add_argument("--output", "-o", default="calendar.ics")
    p.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Retry a failed download this many times with backoff",
    )
    p.add_argument("--calname", help="Override X-WR-CALNAME")
    p.add_argument(
        "--min-events",
        type=int,
        default=0,
        help="Fail instead of writing if fewer than N events survive, so a "
        "transient Outlook error page cannot blank the published calendar",
    )

    p.add_argument("--keep-cancelled", dest="drop_cancelled", action="store_false")
    p.add_argument("--keep-following", dest="drop_following", action="store_false")
    p.add_argument("--keep-declined", dest="drop_declined", action="store_false")
    p.add_argument("--drop-tentative", dest="drop_tentative", action="store_true")
    p.add_argument("--drop-free", dest="drop_free", action="store_true")
    p.add_argument("--drop-allday", dest="keep_allday", action="store_false")
    p.set_defaults(
        drop_cancelled=True,
        drop_following=True,
        drop_declined=True,
        drop_tentative=False,
        drop_free=False,
        keep_allday=True,
    )
    return p


def fetch(url: str, retries: int = 3) -> str:
    """Download the feed, retrying transient Outlook/network failures."""
    req = urllib.request.Request(url, headers={"User-Agent": "ics-calendar-filter/1.0"})
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt == retries:
                break
            delay = 2**attempt
            print(
                f"warning: fetch attempt {attempt}/{retries} failed ({exc}); "
                f"retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise SystemExit(f"error: could not download feed after {retries} attempts: {last}")


def main(argv=None) -> int:
    cfg = build_parser().parse_args(argv)

    if cfg.input:
        with open(cfg.input, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    else:
        url = cfg.url or os.environ.get("ICS_URL")
        if not url:
            print("error: no feed given; pass --url/--input or set $ICS_URL", file=sys.stderr)
            return 2
        text = fetch(url, cfg.retries)

    if "BEGIN:VCALENDAR" not in text:
        print("error: response was not an ICS calendar", file=sys.stderr)
        return 1

    body, stats = filter_calendar(text, cfg)

    if stats["kept"] < cfg.min_events:
        print(
            f"error: only {stats['kept']} events survived, expected at least "
            f"{cfg.min_events}; refusing to overwrite {cfg.output}",
            file=sys.stderr,
        )
        return 1

    if cfg.calname:
        body = re.sub(
            r"(?im)^X-WR-CALNAME:.*$", "X-WR-CALNAME:" + cfg.calname, body, count=1
        )

    with open(cfg.output, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)

    dropped = stats["total"] - stats["kept"]
    print(f"total={stats['total']} kept={stats['kept']} dropped={dropped}")
    for key in ("cancelled", "following", "declined-or-following", "tentative", "free"):
        if stats.get(key):
            print(f"  dropped {key}: {stats[key]}")
    print(f"  EXDATEs added to protect masters: {stats['exdates_added']}")
    print(f"wrote {cfg.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
