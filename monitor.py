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

        for sec in sections:
            if wanted and sec["section"] not in wanted:
                continue

            key = sec["key"]
            open_now = sec["open_seats"] > 0
            was_open = old_state.get(key, {}).get("open", False)

            new_state[key] = {
                "open": open_now,
                "open_seats": sec["open_seats"],
                "capacity": sec["capacity"],
                "enrolled": sec["enrolled"],
            }

            status = "OPEN" if open_now else "full"
            print(f"  {sec['section']:<6} {sec['enrolled']}/{sec['capacity']}"
                  f"  {status}", file=sys.stderr)

            if open_now and (not was_open or always_notify):
                newly_open.append(sec)
            elif was_open and not open_now and notify_on_close:
                newly_closed.append(sec)

        # Be polite between courses.
        time.sleep(1)

    save_json(state_file, new_state)

    # ------------------------------------------------------------- notify
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if newly_open:
        embeds = []
        for sec in newly_open:
            embeds.append({
                "title": f"{sec['key']}",
                "description": (
                    f"**{sec['open_seats']} seat(s) open**\n"
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
    sys.exit(main())
