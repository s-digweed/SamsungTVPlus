"""
status_page.py - shared by the PlexTV and SamsungTVPlus repos.

Writes <out_json> for status.html: every channel per region with its VPN status,
plus a rolling log of channels Added / Removed / Renamed per region per day.

    from status_page import update_status
    update_status("Plex", regions, "status.json", "status_state.json")

regions = { "us": {"name": "United States",
                   "channels": [ {"id":..., "name":..., "logo":..., "group":...,
                                  "status": "free" | "blocked" | "unknown",
                                  "detail": "short text"} , ... ] }, ... }

The state file keeps the previous channel list per region (id -> name) and the
change log; commit it every run or the history is lost.
"""
import json
import os
from datetime import datetime, timedelta, timezone

CHANGE_DAYS = 7          # keep this many days of Added/Removed/Renamed history


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def update_status(service, regions, out_json, state_file, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = _load(state_file, {"regions": {}, "changes": []})
    prev = state.get("regions", {})
    changes = state.get("changes", [])
    first_run = not prev

    for r, info in regions.items():
        cur = {c["id"]: c["name"] for c in info.get("channels", [])}
        if not cur:                       # region failed this run: keep old snapshot, no diff
            continue
        old = prev.get(r)
        if old is None:                   # region seen for the first time: baseline only
            prev[r] = cur
            continue
        for cid, name in cur.items():
            if cid not in old:
                changes.append({"d": today, "r": r, "t": "added", "id": cid, "n": name})
            elif old[cid] != name:
                changes.append({"d": today, "r": r, "t": "renamed", "id": cid, "n": name, "o": old[cid]})
        for cid, name in old.items():
            if cid not in cur:
                changes.append({"d": today, "r": r, "t": "removed", "id": cid, "n": name})
        prev[r] = cur

    cutoff = (now - timedelta(days=CHANGE_DAYS - 1)).strftime("%Y-%m-%d")
    seen, kept = set(), []
    for c in changes:                     # prune old days, drop exact repeats
        key = (c["d"], c["r"], c["t"], c["id"], c.get("n"), c.get("o"))
        if c["d"] >= cutoff and key not in seen:
            seen.add(key); kept.append(c)
    state = {"regions": prev, "changes": kept,
             "since": state.get("since") or today}
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))

    out = {
        "service": service,
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tracking_since": state["since"],
        "first_run": first_run,
        "regions": {
            r: {"name": info.get("name", r.upper()),
                # compact rows: [id, name, logo, group, status, detail]
                "channels": [[c["id"], c["name"], c.get("logo", ""), c.get("group", ""),
                              c.get("status", "unknown"), c.get("detail", "")]
                             for c in sorted(info.get("channels", []), key=lambda c: c["name"].lower())]}
            for r, info in regions.items()
        },
        "changes": sorted(kept, key=lambda c: (c["d"], c["r"], c["t"], c["n"].lower()), reverse=True),
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(i["channels"]) for i in out["regions"].values())
    print(f"Status page data: {out_json} ({n} channel entries, {len(kept)} changes in last {CHANGE_DAYS} days)")
