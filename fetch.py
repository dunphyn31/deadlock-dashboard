#!/usr/bin/env python3
"""
fetch.py - pull a player's most recent ranked Deadlock matches (and full match metadata) into SQLite.

Usage:
    python fetch.py 123456789              # SteamID3 / account id
    python fetch.py https://steamcommunity.com/profiles/76561198083722128
    python fetch.py 123456789 --count 200
    python fetch.py 123456789 --baseline 300   # plus ~300 matches of top-200 NA/EU players

Re-running only downloads metadata for matches not already stored.
"""
import argparse
import json
import re
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests

import db

API = "https://api.deadlock-api.com"
RANKED = 4  # match_mode enum: 1 unranked, 2 private lobby, 3 coop bot, 4 ranked
BATCH = 20  # match ids per bulk-metadata request (endpoint allows 30 req/min per IP)
STEAM64_BASE = 76561197960265728
REGIONS = ("NAmerica", "Europe")
FULL_METADATA = {
    "include_info": "true", "include_objectives": "true", "include_mid_boss": "true",
    "include_player_info": "true", "include_player_kda": "true", "include_player_items": "true",
    "include_player_stats": "true", "include_player_death_details": "true",
}

OBJECTIVE_IDS = [
    "Core", "Tier1Lane1", "Tier1Lane2", "Tier1Lane3", "Tier1Lane4", "Tier2Lane1", "Tier2Lane2",
    "Tier2Lane3", "Tier2Lane4", "Titan", "TitanShieldGenerator1", "TitanShieldGenerator2",
    "BarrackBossLane1", "BarrackBossLane2", "BarrackBossLane3", "BarrackBossLane4",
]

session = requests.Session()
session.headers["User-Agent"] = "deadlock-dashboard/1.0"


def get(path, params=None, timeout=120):
    """GET with retries on rate limiting / load shedding (honours Retry-After)."""
    for attempt in range(6):
        r = session.get(f"{API}{path}", params=params, timeout=timeout)
        if r.status_code in (429, 503, 502, 504):
            wait = float(r.headers.get("Retry-After") or 5 * (attempt + 1))
            print(f"  API busy ({r.status_code}), retrying in {wait:.0f}s ...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def parse_account_id(s):
    """Accept a SteamID3, a SteamID64 or a steamcommunity.com/profiles/<id64> URL."""
    s = str(s).strip()
    m = re.search(r"(\d{5,})", s)
    if not m:
        raise ValueError(f"No numeric Steam id in {s!r} (vanity /id/ URLs can't be resolved; use the number)")
    n = int(m.group(1))
    return n - STEAM64_BASE if n >= STEAM64_BASE else n


# --------------------------------------------------------------------------- normalisers
# The bulk endpoint returns enums as strings ("Team1", "Tier1Lane1"); the single-match endpoint as ints.
def team_int(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return 1 if str(v).endswith("1") else 0


def objective_key(o):
    v = o.get("team_objective", o.get("team_objective_id"))
    if isinstance(v, int):
        return OBJECTIVE_IDS[v] if 0 <= v < len(OBJECTIVE_IDS) else str(v)
    return str(v).replace("_", "")


def xy(p):
    if isinstance(p, dict):
        return p.get("x"), p.get("y")
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        return p[0], p[1]
    return None, None


def store_match(conn, m):
    """Insert one match metadata record (bulk-endpoint shape or single-endpoint match_info shape)."""
    mid = m["match_id"]
    for t in ("matches", "match_players", "player_stats", "objectives", "mid_boss", "deaths", "items"):
        conn.execute(f"DELETE FROM {t} WHERE match_id = ?", (mid,))

    conn.execute(
        "INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mid, m.get("start_time"), m.get("duration_s"), team_int(m.get("winning_team")),
         str(m.get("match_outcome")), m.get("average_badge_team0"), m.get("average_badge_team1"),
         m.get("average_badge"), json.dumps(m.get("banned_hero_ids") or []),
         zlib.compress(json.dumps(m, separators=(",", ":")).encode(), 6)),
    )
    for p in m.get("players", []):
        slot = p["player_slot"]
        conn.execute(
            "INSERT INTO match_players VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, slot, p.get("account_id"), team_int(p.get("team")), p.get("hero_id"), p.get("kills"),
             p.get("deaths"), p.get("assists"), p.get("net_worth"), p.get("last_hits"), p.get("denies"),
             p.get("player_level", p.get("level")), p.get("ability_points"), p.get("assigned_lane"),
             p.get("abandon_match_time_s")),
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO player_stats VALUES ({','.join('?' * (3 + len(db.STAT_COLS)))})",
            [(mid, slot, s["time_stamp_s"], *(s.get(c) for c in db.STAT_COLS)) for s in p.get("stats") or []],
        )
        conn.executemany(
            "INSERT INTO deaths VALUES (?,?,?,?,?,?,?,?)",
            [(mid, slot, d.get("game_time_s"), d.get("killer_player_slot"), d.get("death_duration_s"),
              d.get("time_to_kill_s"), *xy(d.get("death_pos"))) for d in p.get("death_details") or []],
        )
        conn.executemany(
            "INSERT INTO items VALUES (?,?,?,?,?,?)",
            [(mid, slot, i.get("game_time_s"), i.get("item_id"), i.get("upgrade_id"), i.get("sold_time_s"))
             for i in p.get("items") or []],
        )
    conn.executemany(
        "INSERT INTO objectives VALUES (?,?,?,?,?,?,?)",
        [(mid, team_int(o.get("team")), objective_key(o), o.get("destroyed_time_s"),
          o.get("first_damage_time_s"), o.get("player_damage"), o.get("creep_damage"))
         for o in m.get("objectives") or []],
    )
    conn.executemany(
        "INSERT INTO mid_boss VALUES (?,?,?,?)",
        [(mid, team_int(b.get("team_killed")), team_int(b.get("team_claimed")), b.get("destroyed_time_s"))
         for b in m.get("mid_boss") or []],
    )


def store_detail(conn, info):
    """Positions and extra economy stats from the single-match endpoint's match_info."""
    mid = info["match_id"]
    conn.execute("DELETE FROM paths WHERE match_id = ?", (mid,))
    conn.execute("DELETE FROM econ_stats WHERE match_id = ?", (mid,))
    mp = info.get("match_paths") or {}
    xres, yres = float(mp.get("x_resolution") or 16383), float(mp.get("y_resolution") or 16383)
    for p in mp.get("paths") or []:
        n = min(len(p.get(k) or []) for k in ("x_pos", "y_pos", "health", "combat_type", "move_type"))
        x = p["x_min"] + np.asarray(p["x_pos"][:n]) / xres * (p["x_max"] - p["x_min"])
        y = p["y_min"] + np.asarray(p["y_pos"][:n]) / yres * (p["y_max"] - p["y_min"])
        arr = np.stack([x.round(), y.round(), p["health"][:n], p["combat_type"][:n], p["move_type"][:n]])
        conn.execute("INSERT INTO paths VALUES (?,?,?,?)",
                     (mid, p["player_slot"], n, zlib.compress(arr.astype(np.int16).tobytes(), 6)))

    names = {c["id"]: db.CUSTOM_STATS.get(c["name"]) for c in info.get("custom_user_stats") or []}
    rows = []
    for p in info.get("players") or []:
        for s in p.get("stats") or []:
            v = dict.fromkeys(db.ECON_COLS, 0)
            for c in s.get("custom_user_stats") or []:
                if names.get(c["id"]):
                    v[names[c["id"]]] = c["value"]
            for g in s.get("gold_sources") or []:
                if g.get("source") in db.GOLD_SOURCES:
                    v[db.GOLD_SOURCES[g["source"]]] = g.get("gold") or 0
            rows.append((mid, p["player_slot"], s["time_stamp_s"], *(v[c] for c in db.ECON_COLS)))
    conn.executemany(f"INSERT OR REPLACE INTO econ_stats VALUES ({','.join('?' * (3 + len(db.ECON_COLS)))})", rows)


def fetch_details(conn, match_ids, log=print, workers=4):
    """Download single-match metadata (positions, economy stats) for matches that lack it."""
    done = {r[0] for r in conn.execute("SELECT match_id FROM match_detail")}
    todo = [m for m in match_ids if m not in done]
    log(f"  {len(todo)} matches need position/economy detail")

    def one(mid):
        try:
            return mid, get(f"/v1/matches/{mid}/metadata", {"disable_steam": "true"})["match_info"]
        except (requests.RequestException, KeyError):
            return mid, None

    with ThreadPoolExecutor(workers) as pool:
        for i, (mid, info) in enumerate(pool.map(one, todo), 1):
            if info is None:
                conn.execute("INSERT OR REPLACE INTO match_detail VALUES (?,?,?)", (mid, "missing", int(time.time())))
            else:
                if not conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (mid,)).fetchone():
                    store_match(conn, info)
                store_detail(conn, info)
                conn.execute("INSERT OR REPLACE INTO match_detail VALUES (?,?,?)", (mid, "ok", int(time.time())))
            conn.commit()
            if i % 20 == 0 or i == len(todo):
                log(f"  detail {i} of {len(todo)}")


def resolve_leaderboard(regions=REGIONS, top=200, max_candidates=10, min_badge=110, log=print):
    """Top-`top` entries per region. Entries list several candidate accounts sharing the name; keep the one
    with the highest current badge (ties: most recent ranked match). Names with more than `max_candidates`
    candidates (common names, up to hundreds of accounts) are skipped as unresolvable, and so are entries
    whose best candidate is below `min_badge` (Ascendant), which means the real account wasn't among them."""
    entries, skipped = [], 0
    for region in regions:
        for e in get(f"/v1/leaderboard/{region}")["entries"]:
            if e["rank"] > top or not e.get("possible_account_ids"):
                continue
            if len(e["possible_account_ids"]) > max_candidates:
                skipped += 1
                continue
            entries.append((region, e))
    if skipped:
        log(f"  skipped {skipped} entries with ambiguous names (> {max_candidates} candidate accounts)")
    ids = sorted({a for _, e in entries for a in e["possible_account_ids"]})
    log(f"  resolving {len(entries)} leaderboard entries ({len(ids)} candidate accounts) ...")
    ranks = {}
    for i in range(0, len(ids), 100):
        for r in get("/v1/players/rank", {"account_ids": ",".join(map(str, ids[i:i + 100]))}):
            ranks[r["account_id"]] = (r.get("badge") or 0, (r.get("last_match") or {}).get("start_time") or 0)
        if i + 100 < len(ids):
            time.sleep(3.1)  # rank endpoint: 20 req/min
    out = []
    for region, e in entries:
        best = max(e["possible_account_ids"], key=lambda a: ranks.get(a, (0, 0)))
        if ranks.get(best, (0, 0))[0] >= min_badge:
            out.append((region, e["rank"], e["account_name"], best, ranks[best][0]))
    return out


def fetch_baseline(account_id, count=300, regions=REGIONS, top=200, log=print):
    """Replace the comparison set with ~`count` ranked matches played by top-`top` leaderboard players in
    `regions`, from the same period as the tracked player's matches. Only those players' rows are compared."""
    conn = db.connect()
    try:
        t0, t1 = conn.execute("SELECT MIN(start_time), MAX(start_time) FROM history WHERE account_id = ?",
                              (account_id,)).fetchone()
        if t0 is None:
            raise ValueError("Fetch the player's matches first.")
        lb = [r for r in resolve_leaderboard(regions, top, log=log) if r[3] != account_id]
        conn.execute("DELETE FROM leaderboard")
        conn.executemany("INSERT OR REPLACE INTO leaderboard VALUES (?,?,?,?,?,?)",
                         [(*r, int(time.time())) for r in lb])
        conn.execute("DELETE FROM baseline WHERE account_id = ?", (account_id,))
        conn.commit()
        own = {r[0] for r in conn.execute("SELECT match_id FROM history WHERE account_id = ?", (account_id,))}
        picked = []
        for i, region in enumerate(regions):
            ids = [r[3] for r in lb if r[0] == region]
            want = count // len(regions)
            log(f"  {region}: {len(ids)} players, fetching {want} of their ranked matches ...")
            rows = get("/v1/matches/metadata", {
                "match_mode": "ranked", "account_ids": ",".join(map(str, ids)),
                "min_unix_timestamp": t0, "max_unix_timestamp": t1 + 86400,
                "order_by": "start_time", "order_direction": "desc", "limit": want + len(own),
                **FULL_METADATA,
            })
            for m in [m for m in rows if m["match_id"] not in own and m["match_id"] not in picked][:want]:
                store_match(conn, m)
                conn.execute("INSERT OR IGNORE INTO baseline VALUES (?,?)", (m["match_id"], account_id))
                picked.append(m["match_id"])
            conn.commit()
            if i + 1 < len(regions):
                time.sleep(2.2)  # bulk endpoint: 30 req/min
        fetch_details(conn, picked, log)
        log(f"Done: {len(picked)} matches with {len(lb)} top-{top} players.")
        return len(picked)
    finally:
        conn.close()


# --------------------------------------------------------------------------- steps
def refresh_assets(conn):
    heroes = get("/v1/assets/heroes")
    conn.executemany(
        "INSERT OR REPLACE INTO heroes VALUES (?,?,?)",
        [(h["id"], h["name"], (h.get("images") or {}).get("icon_image_small")) for h in heroes],
    )
    items = get("/v1/assets/items")
    conn.executemany(
        "INSERT OR REPLACE INTO item_ref VALUES (?,?,?,?,?,?,?)",
        [(i["id"], i.get("name"), i.get("type"), i.get("item_slot_type"), i.get("item_tier"), i.get("cost"),
          i.get("shop_image_small") or i.get("image")) for i in items],
    )


def refresh_profile(conn, account_id):
    try:
        prof = get("/v1/players/steam", {"account_ids": account_id})
        p = prof[0] if prof else {}
    except requests.RequestException:
        p = {}
    conn.execute(
        "INSERT INTO accounts VALUES (?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
        "persona_name=COALESCE(excluded.persona_name, persona_name), avatar=COALESCE(excluded.avatar, avatar), "
        "last_fetched=excluded.last_fetched",
        (account_id, p.get("personaname"), p.get("avatarmedium"), int(time.time())),
    )


def update(account_id, count=200, log=print):
    """Fetch the latest `count` ranked matches for `account_id`. Returns (ranked_found, newly_stored, failed)."""
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM heroes").fetchone()[0] == 0:
            log("Downloading hero/item assets ...")
            refresh_assets(conn)
        refresh_profile(conn, account_id)

        log(f"Fetching match history for {account_id} ...")
        hist = get(f"/v1/players/{account_id}/match-history")
        ranked = sorted((h for h in hist if h.get("match_mode") == RANKED),
                        key=lambda h: h["start_time"], reverse=True)[:count]
        log(f"  {len(hist)} matches in history, {len(ranked)} ranked kept")
        conn.executemany(
            "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(account_id, h["match_id"], h.get("hero_id"), h.get("hero_level"), h.get("start_time"),
              h.get("player_team"), h.get("match_result"), int(h.get("match_result") == h.get("player_team")),
              h.get("player_kills"), h.get("player_deaths"), h.get("player_assists"), h.get("denies"),
              h.get("net_worth"), h.get("last_hits"), h.get("abandoned_time_s"), h.get("match_duration_s"),
              h.get("player_match_outcome"), h.get("ranked_display_badge"), h.get("ranked_delta"),
              h.get("ranked_calibration_match"), int(bool(h.get("ranked_used_demotion_protection"))))
             for h in ranked],
        )
        conn.commit()

        have = {r[0] for r in conn.execute("SELECT match_id FROM matches")}
        missing = [h["match_id"] for h in ranked if h["match_id"] not in have]
        log(f"  {len(missing)} matches need metadata")
        stored = 0
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            log(f"  metadata {i + 1}-{i + len(chunk)} of {len(missing)} ...")
            rows = get("/v1/matches/metadata", {
                "match_ids": ",".join(map(str, chunk)), "limit": len(chunk), **FULL_METADATA,
            })
            for m in rows:
                store_match(conn, m)
                stored += 1
            conn.commit()
            if i + BATCH < len(missing):
                time.sleep(2.2)  # stay under 30 req/min

        # Anything the bulk (ClickHouse) endpoint lacked: try the single-match endpoint (cache/S3 only).
        have = {r[0] for r in conn.execute("SELECT match_id FROM matches")}
        failed = []
        for mid in [m for m in missing if m not in have]:
            try:
                store_match(conn, get(f"/v1/matches/{mid}/metadata", {"disable_steam": "true"})["match_info"])
                stored += 1
                conn.commit()
            except (requests.RequestException, KeyError) as e:
                failed.append(mid)
                log(f"  no metadata available for match {mid} ({e})")
        fetch_details(conn, [h["match_id"] for h in ranked if h["match_id"] not in failed], log)
        log(f"Done: {stored} new matches stored, {len(failed)} without metadata.")
        return len(ranked), stored, failed
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("player", help="SteamID3, SteamID64 or steamcommunity.com/profiles/... URL")
    ap.add_argument("--count", type=int, default=200, help="number of most recent ranked matches (default 200)")
    ap.add_argument("--baseline", type=int, metavar="N",
                    help="also fetch ~N ranked matches of top-200 NA/EU leaderboard players, for comparison")
    a = ap.parse_args()
    try:
        aid = parse_account_id(a.player)
        update(aid, a.count)
        if a.baseline:
            fetch_baseline(aid, a.baseline)
    except (ValueError, requests.RequestException) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
