#!/usr/bin/env python3
"""
UR Chintai vacancy watcher.

Checks every property in ur_properties.json (Tokyo + Kanagawa) against
UR's internal JSON API, figures out which rooms are newly vacant since
the last run, and emails a rich summary (address, station, pet policy,
size, room type, rent, link) if anything new shows up.

State (which room IDs were vacant last time) is kept in state.json,
which this script rewrites on every run. Property-level static info
(address/station/pet policy) is cached forever in property_static.json
since it doesn't change — it's only fetched the first time a given
property shows a new vacancy, not on every run for all 474 properties.

Both JSON files are committed back to the repo by the GitHub Actions
workflow so state/cache survive between runs.

NOTE on data reliability: the vacancy endpoint (detail_bukken_room) is
well understood and documented at https://duongnt.com/urchintai-api/.
The property-detail endpoints used here for address/station/pet policy
are NOT publicly documented — this script calls them and parses the
response defensively (best-effort field matching), because UR doesn't
publish a field reference. If a field can't be confidently extracted
you'll see "(see listing link)" instead of a wrong value — the listing
link in every email is always accurate and lets you confirm directly.
"""

import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests

API_ROOT = "https://chintai.sumai.ur-net.go.jp/chintai/api/"
ROOM_ENDPOINT = API_ROOT + "bukken/detail/detail_bukken_room/"
BUKKEN_ENDPOINT = API_ROOT + "bukken/detail/detail_bukken_bukken/"
DESIGN_ENDPOINT = API_ROOT + "bukken/detail/detail_bukken_design/"

HERE = Path(__file__).parent
PROPERTIES_FILE = HERE / "ur_properties.json"
STATE_FILE = HERE / "state.json"
STATIC_CACHE_FILE = HERE / "property_static.json"

# Optional filters — edit these to narrow what triggers a notification.
# Leave empty / None to disable a filter.
MADORI_WHITELIST = []       # e.g. ["1LDK", "2DK", "2LDK"] — empty = any layout
MAX_RENT_YEN = None         # e.g. 150000 — empty = no cap
REQUEST_DELAY_SEC = 0.4     # be polite to UR's servers between requests

ADDRESS_KEY_HINTS = ["address", "syozai", "addr", "place", "location"]
STATION_KEY_HINTS = ["traffic", "eki", "station", "access", "kotsu"]
PET_KEYWORDS = ["ペット可", "ペット共生", "ペット飼育可"]


def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_properties():
    with open(PROPERTIES_FILE, encoding="utf-8") as f:
        return json.load(f)


def api_post(session, endpoint, payload):
    try:
        resp = session.post(endpoint, data=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  ! request failed for {endpoint}: {e}", file=sys.stderr)
        return None


def fetch_vacant_rooms(session, shisya, danchi, shikibetu):
    """Return a list of room dicts (possibly empty) for one property, or
    None if the request itself failed (distinct from 'no vacancies')."""
    payload = {
        "shisya": shisya,
        "danchi": danchi,
        "shikibetu": shikibetu,
        "orderByField": "0",
        "orderBySort": "0",
        "pageIndex": "0",
    }
    data = api_post(session, ROOM_ENDPOINT, payload)
    if data is None:
        return None
    return data or []


def _walk_strings(obj):
    """Yield every (key_path, string_value) pair found anywhere in a
    nested dict/list, for heuristic field-matching against undocumented
    API responses."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and v.strip():
                yield k, v.strip()
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)


def extract_address(blob):
    for key, val in _walk_strings(blob):
        key_l = key.lower()
        if any(h in key_l for h in ADDRESS_KEY_HINTS):
            return val
    # Fallback: look for a string that looks like a Japanese address
    # (contains a prefecture/ward/city marker and isn't too long).
    for _, val in _walk_strings(blob):
        if len(val) < 60 and re.search(r"(都|道府|県).{0,20}(区|市|町|村)", val):
            return val
    return None


def extract_station(blob):
    for key, val in _walk_strings(blob):
        key_l = key.lower()
        if any(h in key_l for h in STATION_KEY_HINTS):
            return val
    for _, val in _walk_strings(blob):
        if "駅" in val and ("分" in val or "徒歩" in val):
            return val
    return None


def extract_pet_friendly(blob):
    text = json.dumps(blob, ensure_ascii=False)
    for kw in PET_KEYWORDS:
        if kw in text:
            return True
    return False


def fetch_property_static(session, prop):
    """Fetch + cache address/station/pet info for one property. Cached
    forever (this data doesn't change), so this only runs once per
    property, the first time it ever shows a new vacancy."""
    payload = {
        "shisya": prop["shisya"],
        "danchi": prop["danchi"],
        "shikibetu": prop["shikibetu"],
    }
    bukken_data = api_post(session, BUKKEN_ENDPOINT, payload)
    time.sleep(REQUEST_DELAY_SEC)
    design_data = api_post(session, DESIGN_ENDPOINT, {"bukkenid": f'{prop["shisya"]}_{prop["danchi"]}'})
    time.sleep(REQUEST_DELAY_SEC)

    combined = {"bukken": bukken_data, "design": design_data}
    return {
        "address": extract_address(combined) or "(see listing link)",
        "station": extract_station(combined) or "(see listing link)",
        "pet_friendly": extract_pet_friendly(combined),
    }


def get_property_static(session, prop, cache):
    key = f'{prop["shisya"]}_{prop["danchi"]}{prop["shikibetu"]}'
    if key in cache:
        return cache[key]
    try:
        info = fetch_property_static(session, prop)
    except Exception as e:
        print(f"  ! couldn't fetch static info for {prop['name']}: {e}", file=sys.stderr)
        info = {"address": "(see listing link)", "station": "(see listing link)", "pet_friendly": False}
    cache[key] = info
    return info


def room_passes_filters(room):
    if MADORI_WHITELIST and room.get("type") not in MADORI_WHITELIST:
        return False
    if MAX_RENT_YEN is not None:
        rent_digits = "".join(ch for ch in (room.get("rent") or "") if ch.isdigit())
        if rent_digits and int(rent_digits) > MAX_RENT_YEN:
            return False
    return True


def send_email(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("NOTIFY_TO", user)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


def format_entry(prop, room, static_info):
    pref_label = "東京都" if prop["pref"] == "tokyo" else "神奈川県"
    pet = "Pet-friendly" if static_info["pet_friendly"] else "Not confirmed pet-friendly"
    return (
        f"■ {prop['name']} ({pref_label})\n"
        f"  Address:   {static_info['address']}\n"
        f"  Station:   {static_info['station']}\n"
        f"  Pet policy:{pet}\n"
        f"  Room type: {room.get('type', '?')}\n"
        f"  Size:      {room.get('floorspace', '?')}\n"
        f"  Rent:      {room.get('rent', '?')} (common fee {room.get('commonfee', '?')})\n"
        f"  Floor:     {room.get('floor', '?')}\n"
        f"  Listing:   {prop['url']}\n"
    )


def main():
    properties = load_properties()
    state = load_json(STATE_FILE, {})
    static_cache = load_json(STATIC_CACHE_FILE, {})
    new_state = {}
    newly_vacant = []  # list of (property, room) tuples
    checked = 0
    errors = 0

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; personal-vacancy-watch/1.0)",
        "Referer": "https://www.ur-net.go.jp/chintai/",
    })

    for prop in properties:
        key = f'{prop["shisya"]}_{prop["danchi"]}{prop["shikibetu"]}'
        rooms = fetch_vacant_rooms(session, prop["shisya"], prop["danchi"], prop["shikibetu"])
        time.sleep(REQUEST_DELAY_SEC)

        if rooms is None:
            # Couldn't check this run — keep previous known state so we don't
            # lose track of it, and don't treat it as "all vacancies gone".
            new_state[key] = state.get(key, [])
            errors += 1
            continue

        checked += 1
        prev_ids = set(state.get(key, []))
        current_ids = set()

        for room in rooms:
            room_id = room.get("id")
            if not room_id:
                continue
            current_ids.add(room_id)
            if room_id not in prev_ids and room_passes_filters(room):
                newly_vacant.append((prop, room))

        new_state[key] = sorted(current_ids)

    save_json(STATE_FILE, new_state)

    print(f"Checked {checked}/{len(properties)} properties ({errors} errors).")
    print(f"Newly vacant rooms passing filters: {len(newly_vacant)}")

    if not newly_vacant:
        save_json(STATIC_CACHE_FILE, static_cache)
        return

    # Only now do we fetch address/station/pet info — and only for the
    # properties that actually have something new, using the cache so
    # a property already looked up in a past run costs nothing extra.
    entries = []
    for prop, room in newly_vacant:
        static_info = get_property_static(session, prop, static_cache)
        entries.append(format_entry(prop, room, static_info))

    save_json(STATIC_CACHE_FILE, static_cache)

    body = f"{len(newly_vacant)} new UR vacancy(ies) found:\n\n" + "\n".join(entries)
    print(body)

    try:
        send_email(f"UR新着空室 {len(newly_vacant)}件", body)
        print("Email sent.")
    except Exception as e:
        print(f"! Failed to send email: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
