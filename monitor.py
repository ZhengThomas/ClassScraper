#!/usr/bin/env python3
"""
UWaterloo course seat monitor.

Checks the Open Data API for open seats in configured courses and posts to a
Discord webhook when a section opens up. Designed to be run on a schedule
(GitHub Actions cron) with no persistent server.

Environment variables required:
    UW_API_KEY            your Open Data API key
    DISCORD_WEBHOOK_URL   the full webhook URL from Discord

Optional:
    NOTIFY_ON_CLOSE       set to "1" to also notify when a section fills up
    ALWAYS_NOTIFY         set to "1" to notify every run if seats are open,
                          not just on the 0 -> open transition
    STATE_FILE            path to state file (default: state.json)
    COURSES_FILE          path to course config (default: courses.json)

Usage:
    python monitor.py                      check watchlist, notify on changes
    python monitor.py --list CS 479        list every section of a course
    python monitor.py --list CS 479 --raw  same, as raw JSON

Stdlib only -- no pip install needed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://openapi.data.uwaterloo.ca/v3"
USER_AGENT = "personal-course-monitor/1.0"

# The API's exact field names are the one thing I couldn't verify, so we try a
# range of plausible spellings. Matching is done on a normalized key (lowercased,
# underscores stripped) so enrollment_capacity and enrollmentCapacity both hit.
CAPACITY_KEYS = [
    "maxenrollmentcapacity",
    "enrollmentcapacity",
    "capacity",
    "classcapacity",
]
ENROLLED_KEYS = [
    "enrolledstudents",
    "enrollmenttotal",
    "totalenrolled",
    "enrolled",
]
SECTION_KEYS = ["classsection", "sectionname", "section"]
CLASSNUM_KEYS = ["classnumber", "classnumberid", "classid"]
COMPONENT_KEYS = ["coursecomponent", "component", "sectiontype"]


# ---------------------------------------------------------------- http helpers


def _normalize(key):
    return key.lower().replace("_", "").replace("-", "").replace(" ", "")


def pick(record, candidates):
    """Find the first matching key in a dict, ignoring case/underscores."""
    normalized = {_normalize(k): v for k, v in record.items()}
    for cand in candidates:
        if cand in normalized and normalized[cand] is not None:
            return normalized[cand]
    return None


def section_label(value):
    """
    Normalize a section label for comparison.

    The API may report section 1 as 1, "1", or "001" depending on the field,
    so compare on a canonical form. Non-numeric labels (rare, but e.g. "A01")
    fall back to a cased-and-stripped string.
    """
    s = str(value).strip().upper()
    return s.lstrip("0") or "0" if s.isdigit() else s


def section_matches(value, wanted):
    """True if `value` matches any entry in the `wanted` list, tolerantly."""
    target = section_label(value)
    return any(section_label(w) == target for w in wanted)


def api_get(path, api_key, max_retries=3):
    """GET from the Open Data API with backoff on rate limits."""
    url = f"{BASE}{path}"
    req = urllib.request.Request(
        url,
        headers={
            "X-API-KEY": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]

            if e.code == 429:
                # Rate limited. Back off and retry rather than giving up --
                # the limit is per minute, so waiting is usually enough.
                wait = 20 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            if e.code == 401:
                raise RuntimeError(
                    "401 Unauthorized -- check UW_API_KEY is set correctly "
                    "and the registration email was confirmed."
                )

            if e.code == 404:
                raise RuntimeError(
                    f"404 for {path} -- either the course doesn't exist this "
                    f"term, or the endpoint path has changed. Check "
                    f"https://openapi.data.uwaterloo.ca/api-docs/"
                )

            raise RuntimeError(f"HTTP {e.code} for {path}: {body}")

        except urllib.error.URLError as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Network error: {e.reason}")
            time.sleep(5 * (attempt + 1))

    raise RuntimeError(f"Gave up on {path} after {max_retries} attempts")


def post_discord(webhook_url, content, embeds=None):
    """Post a message to a Discord webhook."""
    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds[:10]  # Discord caps at 10 embeds

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        if e.code == 429:
            # Discord rate limit -- wait out the retry_after and try once more.
            try:
                retry_after = json.loads(body).get("retry_after", 5)
            except Exception:
                retry_after = 5
            time.sleep(float(retry_after) + 1)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status
        raise RuntimeError(f"Discord webhook failed ({e.code}): {body}")


# ------------------------------------------------------------- data extraction


def extract_sections(raw, subject, catalog):
    """
    Turn the API response into a flat list of section dicts.

    Returns list of: {key, section, component, class_number, capacity,
                      enrolled, open_seats}
    """
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise RuntimeError(f"Unexpected response shape: {type(raw).__name__}")

    sections = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue

        capacity = pick(rec, CAPACITY_KEYS)
        enrolled = pick(rec, ENROLLED_KEYS)

        if capacity is None or enrolled is None:
            # Couldn't find the fields. Surface the available keys so this is
            # a one-line fix rather than a mystery.
            raise RuntimeError(
                f"Could not find capacity/enrollment fields for "
                f"{subject} {catalog}.\n"
                f"Available keys: {sorted(rec.keys())}\n"
                f"Add the right ones to CAPACITY_KEYS / ENROLLED_KEYS."
            )

        try:
            capacity = int(capacity)
            enrolled = int(enrolled)
        except (TypeError, ValueError):
            continue

        section_name = pick(rec, SECTION_KEYS) or "?"
        component = pick(rec, COMPONENT_KEYS) or ""
        class_number = pick(rec, CLASSNUM_KEYS) or "?"

        sections.append({
            "key": f"{subject} {catalog} {section_name} ({class_number})",
            "section": section_name,
            "component": component,
            "class_number": class_number,
            "capacity": capacity,
            "enrolled": enrolled,
            "open_seats": max(0, capacity - enrolled),
        })

    return sections


# -------------------------------------------------------------- state handling


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"warning: could not read {path} ({e}), using default",
              file=sys.stderr)
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


# ------------------------------------------------------------------ --list


def meeting_summary(rec):
    """
    Best-effort "days time instructor" for a section.

    The nested schedule object is the least predictable part of the response,
    so every lookup is tolerant and the whole thing degrades to "" rather than
    raising -- `--list` is a diagnostic tool and must never be the thing that
    breaks. Use --raw when this comes back empty.
    """
    data = pick(rec, ["scheduledata", "schedule", "meetings"]) or []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return ""

    def hhmm(v):
        s = str(v or "")
        return s[11:16] if "T" in s else s[:5]

    slots, instructors = [], []
    for m in data:
        if not isinstance(m, dict):
            continue
        days = pick(m, ["classmeetingdaypatterncode", "daypattern", "days"]) or ""
        start = hhmm(pick(m, ["classmeetingstarttime", "starttime"]))
        end = hhmm(pick(m, ["classmeetingendtime", "endtime"]))
        window = f"{start}-{end}" if start and end else start or end
        slot = " ".join(p for p in [str(days), window] if p).strip()
        if slot and slot not in slots:
            slots.append(slot)

        people = pick(m, ["instructordata", "instructors"]) or []
        if isinstance(people, dict):
            people = [people]
        for person in people if isinstance(people, list) else []:
            if isinstance(person, dict):
                first = pick(person, ["instructorfirstname", "firstname"]) or ""
                last = pick(person, ["instructorlastname", "lastname"]) or ""
                name = f"{first} {last}".strip() or str(
                    pick(person, ["instructor", "name"]) or "")
            else:
                name = str(person)
            if name and name not in instructors:
                instructors.append(name)

    out = "; ".join(slots[:2])
    if instructors:
        out = f"{out}  {', '.join(instructors[:2])}".strip()
    return out


def list_sections(argv):
    """Dump every section of one course, so you can find section numbers."""
    usage = "usage: python monitor.py --list SUBJECT CATALOG [--raw]"
    argv = [str(a) for a in argv]
    positional = [a for a in argv if not a.startswith("--")]
    if len(positional) < 2:
        raise SystemExit(usage)

    api_key = os.environ.get("UW_API_KEY")
    if not api_key:
        raise SystemExit("UW_API_KEY is not set")

    subject, catalog = positional[0].upper(), str(positional[1])

    # Term comes from courses.json so it stays in one place; TERM_CODE
    # overrides for a one-off lookup in a different term.
    config = load_json(os.environ.get("COURSES_FILE", "courses.json"), {}) or {}
    term = str(os.environ.get("TERM_CODE") or config.get("term") or "").strip()
    if not term:
        raise SystemExit('No term found: set TERM_CODE or add "term" to courses.json')

    raw = api_get(f"/ClassSchedules/{term}/{subject}/{catalog}", api_key)

    if "--raw" in argv:
        print(json.dumps(raw, indent=2, sort_keys=True))
        return 0

    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        print(f"No sections returned for {subject} {catalog} in term {term}.")
        return 1

    print(f"\n{subject} {catalog}  --  term {term}  --  {len(raw)} section(s)\n")
    head = f"{'SECTION':<9}{'COMP':<7}{'CLASS#':<9}{'SEATS':<11}WHEN / WHO"
    print(head)
    print("-" * max(len(head), 60))

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        cap = pick(rec, CAPACITY_KEYS)
        enr = pick(rec, ENROLLED_KEYS)
        section = pick(rec, SECTION_KEYS)
        seats = "?"
        if cap is not None and enr is not None:
            try:
                free = int(cap) - int(enr)
                seats = f"{enr}/{cap}" + (f" +{free}" if free > 0 else "")
            except (TypeError, ValueError):
                seats = f"{enr}/{cap}"
        print(f"{str(section or '?'):<9}"
              f"{str(pick(rec, COMPONENT_KEYS) or '?'):<7}"
              f"{str(pick(rec, CLASSNUM_KEYS) or '?'):<9}"
              f"{seats:<11}{meeting_summary(rec)}")

    print('\nPut the SECTION values you want in courses.json under "sections".')
    print("Run with --raw to see the full JSON (useful if columns show '?').")
    return 0


# ---------------------------------------------------------------------- main


def main():
    api_key = os.environ.get("UW_API_KEY")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")

    if not api_key:
        raise SystemExit("UW_API_KEY is not set")
    if not webhook:
        raise SystemExit("DISCORD_WEBHOOK_URL is not set")

    state_file = os.environ.get("STATE_FILE", "state.json")
    courses_file = os.environ.get("COURSES_FILE", "courses.json")
    notify_on_close = os.environ.get("NOTIFY_ON_CLOSE") == "1"
    always_notify = os.environ.get("ALWAYS_NOTIFY") == "1"

    config = load_json(courses_file, None)
    if not config:
        raise SystemExit(f"No course config found at {courses_file}")

    term = str(config["term"])
    watchlist = config["courses"]

    old_state = load_json(state_file, {})
    new_state = {}
    newly_open = []
    newly_closed = []
    errors = []
    config_warnings = []

    for course in watchlist:
        subject = course["subject"].upper()
        catalog = str(course["catalog"])
        # Optional: only watch specific sections, e.g. ["001", "002"]
        wanted = course.get("sections")

        label = f"{subject} {catalog}"
        print(f"checking {label}...", file=sys.stderr)

        try:
            raw = api_get(
                f"/ClassSchedules/{term}/{subject}/{catalog}", api_key
            )
            sections = extract_sections(raw, subject, catalog)
        except RuntimeError as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            errors.append(f"**{label}**: {e}")
            # Carry forward old state so a transient failure doesn't cause a
            # false "newly open" alert on the next successful run.
            for k, v in old_state.items():
                if k.startswith(label):
                    new_state[k] = v
            continue

        if wanted:
            available = [s["section"] for s in sections]
            missing = [w for w in wanted
                       if not any(section_matches(a, [w]) for a in available)]
            if missing:
                # Silently watching nothing looks identical to "no seats open",
                # so make a bad section filter loud.
                msg = (f"section(s) {missing} not found for {label}. "
                       f"API reports: {available}")
                print(f"  WARNING: {msg}", file=sys.stderr)
                # Not an `error`: this is a config mistake that persists every
                # run, so it must not spam Discord hourly or fail the job.
                config_warnings.append(f"**{label}**: {msg}")

        for sec in sections:
            if wanted and not section_matches(sec["section"], wanted):
                continue

            key = sec["key"]
            open_now = sec["open_seats"] > 0
            prev = old_state.get(key, {})
            was_open = prev.get("open", False)
            was_seats = prev.get("open_seats", 0)
            # Shown in the alert so "newly open" reads differently from
            # "more seats than last time".
            sec["prev_open_seats"] = was_seats
            sec["first_seen"] = not prev

            new_state[key] = {
                "open": open_now,
                "open_seats": sec["open_seats"],
                "capacity": sec["capacity"],
                "enrolled": sec["enrolled"],
            }

            status = "OPEN" if open_now else "full"
            print(f"  {sec['section']:<6} {sec['enrolled']}/{sec['capacity']}"
                  f"  {status}", file=sys.stderr)

            # Notify on any *increase* in open seats, not just the full -> open
            # transition. Catches reserved-seat blocks being released, which
            # can show up as either enrolled dropping or capacity rising.
            more_seats = sec["open_seats"] > was_seats
            if open_now and (not was_open or more_seats or always_notify):
                newly_open.append(sec)
            elif was_open and not open_now and notify_on_close:
                newly_closed.append(sec)

        # Be polite between courses.
        time.sleep(1)

    # Reserved key -- never collides with a section key ("SUBJ CAT SEC (NUM)").
    prior_warnings = old_state.get("_config_warnings", [])
    new_state["_config_warnings"] = config_warnings
    save_json(state_file, new_state)

    # ------------------------------------------------------------- notify
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if newly_open:
        embeds = []
        for sec in newly_open:
            prev_seats = sec.get("prev_open_seats", 0)
            if sec.get("first_seen"):
                headline = f"**{sec['open_seats']} seat(s) open**"
            elif prev_seats == 0:
                headline = f"**{sec['open_seats']} seat(s) open** (was full)"
            else:
                headline = (f"**{sec['open_seats']} seat(s) open** "
                            f"(was {prev_seats})")
            embeds.append({
                "title": f"{sec['key']}",
                "description": (
                    f"{headline}\n"
                    f"Enrolled: {sec['enrolled']} / {sec['capacity']}\n"
                    f"Component: {sec['component'] or 'n/a'}\n"
                    f"Class number: `{sec['class_number']}`"
                ),
                "color": 0x2ECC71,
                "footer": {"text": now},
            })
        post_discord(webhook, "@here Seat available!", embeds)
        print(f"notified: {len(newly_open)} section(s) open", file=sys.stderr)

    if newly_closed:
        embeds = [{
            "title": sec["key"],
            "description": f"Filled up ({sec['enrolled']}/{sec['capacity']})",
            "color": 0xE74C3C,
            "footer": {"text": now},
        } for sec in newly_closed]
        post_discord(webhook, "Section closed.", embeds)

    # Only ping when the warning set changes, so a standing config mistake
    # doesn't repost every hour.
    if config_warnings and config_warnings != prior_warnings:
        post_discord(
            webhook,
            ":grey_question: Check your courses.json:\n"
            + "\n".join(config_warnings)[:1800],
        )

    if errors:
        post_discord(
            webhook,
            ":warning: Monitor hit errors:\n" + "\n".join(errors)[:1800],
        )

    if not newly_open and not newly_closed and not errors:
        print("no changes", file=sys.stderr)

    # Non-zero exit if everything failed, so the CI run shows as failed.
    if errors and len(errors) == len(watchlist):
        return 1
    return 0


if __name__ == "__main__":
    if "--list" in sys.argv:
        idx = sys.argv.index("--list")
        sys.exit(list_sections(sys.argv[idx + 1:]))
    sys.exit(main())
