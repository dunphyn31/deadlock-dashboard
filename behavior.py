"""Death categories, kill impact and economy metrics for every player in stored matches.

Positions come from the per-second match paths (world units). Health 0 means dead (position snaps to base).
"""
import zlib

import numpy as np
import pandas as pd

import db

LANES = {1: "Yellow", 4: "Blue", 6: "Green"}
TOP = "Top 200 NA/EU"
GROUPS = ["You", "Teammates", "Enemies", TOP]


def load_paths(conn, match_id):
    out = {}
    for slot, n, blob in conn.execute("SELECT player_slot, n, data FROM paths WHERE match_id = ?", (match_id,)):
        out[slot] = np.frombuffer(zlib.decompress(blob), dtype=np.int16).reshape(5, n)
    return out


def match_sides(paths, teams):
    """(midline_y, +1/-1 direction from midline toward each team's enemy base) from spawn positions."""
    spawn = {}
    for t in (0, 1):
        ys = [p[1, 3:15][p[2, 3:15] > 0] for s, p in paths.items() if teams.get(s) == t and p.shape[1] > 15]
        ys = np.concatenate(ys) if ys else np.array([])
        spawn[t] = np.median(ys) if len(ys) else (-1 if t == 0 else 1) * 9000
    mid = (spawn[0] + spawn[1]) / 2
    return mid, {t: np.sign(spawn[1 - t] - mid) or (1 if t == 0 else -1) for t in (0, 1)}


def team_objective_times(obj, boss, team, with_boss):
    """Times at which `team` destroyed an enemy objective (and optionally claimed the mid boss)."""
    t = obj.loc[obj["team"] != team, "destroyed_time_s"].to_numpy()
    if with_boss and len(boss):
        t = np.concatenate([t, boss.loc[boss["team_claimed"] == team, "destroyed_time_s"].to_numpy()])
    return np.sort(t)


def any_in(times, lo, hi):
    i = np.searchsorted(times, lo)
    return i < len(times) and times[i] <= hi


def analyze_match(mid, paths, players, deaths, obj, boss, p):
    """Return (death rows, kill-participation rows) for one match.

    p: dict(radius, caught_window, side_margin, obj_before, obj_after, impact_window, assist_radius, with_boss)
    """
    teams = dict(zip(players["player_slot"], players["team"]))
    if not paths or len(paths) < 10:
        return [], []
    mid_y, toward_enemy = match_sides(paths, teams)
    obj_times = {t: team_objective_times(obj, boss, t, p["with_boss"]) for t in (0, 1)}

    def pos(slot, s0, s1):
        a = paths.get(slot)
        if a is None:
            return None
        s0, s1 = max(s0, 0), min(s1, a.shape[1] - 1)
        if s1 < s0:
            return None
        return a[:, s0:s1 + 1]

    death_rows, kp_rows = [], []
    for d in deaths.itertuples():
        t, v = int(d.game_time_s), d.player_slot
        vt = teams.get(v)
        if vt is None:
            continue
        x, y = d.x, d.y
        if pd.isna(x):
            vp = pos(v, t, t)
            if vp is None:
                continue
            x, y = vp[0, 0], vp[1, 0]

        # Caught out: no living teammate within radius of the victim at any second in [t - window, t].
        w0 = t - p["caught_window"]
        me = pos(v, w0, t)
        nearest = np.inf
        if me is not None:
            for s, tm in teams.items():
                if tm != vt or s == v:
                    continue
                o = pos(s, w0, t)
                if o is None:
                    continue
                k = min(o.shape[1], me.shape[1])
                alive = (o[2, :k] > 0) & (me[2, :k] > 0)
                if alive.any():
                    dist = np.hypot(o[0, :k] - me[0, :k], o[1, :k] - me[1, :k])[alive]
                    nearest = min(nearest, float(dist.min()))
        caught = nearest > p["radius"]

        # Overextended: died past the midline on the enemy half with no objective taken around the death.
        depth = (y - mid_y) * toward_enemy[vt]
        enemy_side = depth > p["side_margin"]
        obj_near = any_in(obj_times[vt], t - p["obj_before"], t + p["obj_after"])
        over = enemy_side and not obj_near

        death_rows.append(dict(
            match_id=mid, player_slot=v, team=vt, t=t, x=float(x), y=float(y), killer_slot=d.killer_player_slot,
            nearest_teammate=nearest, caught_out=caught, enemy_side=enemy_side, depth=float(depth),
            objective_nearby=obj_near, overextended=over, death_duration_s=d.death_duration_s))

        # Kill participation for the victim's enemies: the killer plus enemies near the death spot.
        kt = 1 - vt
        i = np.searchsorted(obj_times[kt], t)
        next_obj = float(obj_times[kt][i] - t) if i < len(obj_times[kt]) else np.inf
        for s, tm in teams.items():
            if tm != kt:
                continue
            is_killer = s == d.killer_player_slot
            near = False
            if not is_killer:
                o = pos(s, t - 3, t)
                if o is not None:
                    alive = o[2] > 0
                    near = bool(alive.any() and np.hypot(o[0] - x, o[1] - y)[alive].min() <= p["assist_radius"])
            if is_killer or near:
                kp_rows.append(dict(match_id=mid, player_slot=s, team=kt, t=t, victim_slot=v, is_killer=is_killer,
                                    next_objective_s=next_obj, low_impact=next_obj > p["impact_window"]))
    return death_rows, kp_rows


def analyze(conn, match_ids, params):
    """Death and kill-participation event tables for all players in `match_ids` that have paths."""
    ids = tuple(int(m) for m in match_ids) or (-1,)
    ph = ",".join("?" * len(ids))
    q = lambda sql: pd.read_sql_query(sql, conn, params=ids)
    players = q(f"SELECT match_id, player_slot, team FROM match_players WHERE match_id IN ({ph})")
    deaths = q(f"SELECT * FROM deaths WHERE match_id IN ({ph})")
    obj = q(f"SELECT * FROM objectives WHERE match_id IN ({ph}) AND destroyed_time_s > 0")
    boss = q(f"SELECT * FROM mid_boss WHERE match_id IN ({ph})")
    have = {r[0] for r in conn.execute(f"SELECT DISTINCT match_id FROM paths WHERE match_id IN ({ph})", ids)}

    by = lambda df: dict(tuple(df.groupby("match_id")))
    P, D, O, B = by(players), by(deaths), by(obj), by(boss)
    empty = lambda df: df.iloc[0:0]
    drows, krows = [], []
    for mid in ids:
        if mid not in have or mid not in P:
            continue
        a, b = analyze_match(mid, load_paths(conn, mid), P[mid], D.get(mid, empty(deaths)),
                             O.get(mid, empty(obj)), B.get(mid, empty(boss)), params)
        drows += a
        krows += b
    return pd.DataFrame(drows), pd.DataFrame(krows), have


def economy(conn, match_ids):
    """Per player-match economy metrics plus per-snapshot unspent-souls rates."""
    ids = tuple(int(m) for m in match_ids) or (-1,)
    ph = ",".join("?" * len(ids))
    snaps = pd.read_sql_query(f"""
        SELECT ps.match_id, ps.player_slot, ps.time_stamp_s, ps.net_worth, ps.gold_death_loss,
               ps.gold_lane_creep, ps.gold_lane_creep_orbs, ps.gold_neutral_creep, ps.gold_player,
               ps.gold_boss, ps.gold_treasure, ps.gold_denied, ps.creep_kills, ps.possible_creeps, ps.deaths,
               e.unspent_gold_minutes, e.unspent_ap_minutes, e.powerup_gold, e.comeback_gold, e.gold_assists,
               e.gold_team_bonus, e.gold_breakable
        FROM player_stats ps JOIN econ_stats e USING (match_id, player_slot, time_stamp_s)
        WHERE ps.match_id IN ({ph}) ORDER BY ps.match_id, ps.player_slot, ps.time_stamp_s""", conn, params=ids)
    if snaps.empty:
        return pd.DataFrame(), pd.DataFrame()

    g = snaps.groupby(["match_id", "player_slot"])
    # "Unspent Gold Minutes" accumulates souls held x minutes, so its slope is the average souls held.
    dt = g["time_stamp_s"].diff().fillna(snaps["time_stamp_s"]) / 60
    snaps["unspent_avg"] = g["unspent_gold_minutes"].diff().fillna(snaps["unspent_gold_minutes"]) / dt
    snaps["unspent_ap_avg"] = g["unspent_ap_minutes"].diff().fillna(snaps["unspent_ap_minutes"]) / dt
    snaps["t_mid_min"] = (snaps["time_stamp_s"] - dt * 30) / 60

    last = g.tail(1).set_index(["match_id", "player_slot"])
    minutes = last["time_stamp_s"] / 60
    lane = last["gold_lane_creep"].replace(0, np.nan)
    e = pd.DataFrame({
        "souls_per_min": last["net_worth"] / minutes,
        "avg_unspent_souls": last["unspent_gold_minutes"] / minutes,
        "avg_unspent_ap": last["unspent_ap_minutes"] / minutes,
        "souls_lost_on_death": last["gold_death_loss"],
        "souls_lost_per_death": last["gold_death_loss"] / last["deaths"].replace(0, np.nan),
        "orb_secure_share": last["gold_lane_creep_orbs"] / lane,
        "last_hit_rate": last["creep_kills"] / last["possible_creeps"].replace(0, np.nan),
        "souls_lane": last["gold_lane_creep"], "souls_neutrals": last["gold_neutral_creep"],
        "souls_kills": last["gold_player"], "souls_assists": last["gold_assists"],
        "souls_bosses": last["gold_boss"], "souls_treasure": last["gold_treasure"] + last["gold_breakable"],
        "souls_powerups": last["powerup_gold"], "souls_comeback": last["comeback_gold"],
        "souls_denies": last["gold_denied"], "souls_team_bonus": last["gold_team_bonus"],
    }).reset_index()
    return e, snaps


def player_matches(conn, account_id, own_ids, baseline_ids, deaths, kps, econ):
    """One row per (match, player) labelled with comparison group, with behaviour + economy metrics."""
    ids = tuple(int(m) for m in [*own_ids, *baseline_ids]) or (-1,)
    ph = ",".join("?" * len(ids))
    mp = pd.read_sql_query(f"""
        SELECT mp.match_id, mp.player_slot, mp.account_id, mp.team, mp.hero_id, he.name AS hero, mp.kills,
               mp.deaths, mp.assists, mp.net_worth, mp.assigned_lane, m.duration_s, m.winning_team,
               m.average_badge
        FROM match_players mp JOIN matches m USING (match_id) LEFT JOIN heroes he ON he.id = mp.hero_id
        WHERE mp.match_id IN ({ph})""", conn, params=ids)
    me = mp[mp["account_id"] == account_id].set_index("match_id")["team"]
    own = set(own_ids)
    top = {r[0] for r in conn.execute("SELECT account_id FROM leaderboard")}
    mp["group"] = np.select(
        [mp["account_id"] == account_id, mp["match_id"].isin(own) & (mp["team"] == mp["match_id"].map(me)),
         mp["match_id"].isin(own), mp["account_id"].isin(top)],
        ["You", "Teammates", "Enemies", TOP], "")
    # In baseline matches only the leaderboard players themselves are compared.
    mp = mp[mp["group"] != ""]
    lb = pd.read_sql_query("SELECT account_id, region, rank AS lb_rank, account_name FROM leaderboard", conn)
    mp = mp.merge(lb.drop_duplicates("account_id"), on="account_id", how="left")
    mp["won"] = (mp["team"] == mp["winning_team"]).astype(int)
    mp["lane"] = mp["assigned_lane"].map(LANES)
    key = ["match_id", "player_slot"]
    if not deaths.empty:
        dc = deaths.groupby(key).agg(caught_out=("caught_out", "sum"), overextended=("overextended", "sum"),
                                     enemy_side_deaths=("enemy_side", "sum"),
                                     time_dead_s=("death_duration_s", "sum")).reset_index()
        mp = mp.merge(dc, on=key, how="left")
    if not kps.empty:
        kc = kps.groupby(key).agg(kill_participations=("low_impact", "size"),
                                  low_impact_kp=("low_impact", "sum")).reset_index()
        mp = mp.merge(kc, on=key, how="left")
    for c in ["caught_out", "overextended", "enemy_side_deaths", "time_dead_s", "kill_participations", "low_impact_kp"]:
        mp[c] = mp[c].fillna(0) if c in mp else 0
    mp["low_impact_share"] = mp["low_impact_kp"] / mp["kill_participations"].replace(0, np.nan)
    mp["caught_out_share"] = mp["caught_out"] / mp["deaths"].replace(0, np.nan)
    mp["overextended_share"] = mp["overextended"] / mp["deaths"].replace(0, np.nan)
    if not econ.empty:
        mp = mp.merge(econ, on=key, how="left")
    return mp.merge(combat_stats(conn, ids), on=key, how="left")


def combat_stats(conn, ids):
    """Per player-match rates from the stat snapshots: per-10-min K/D/A, damage, accuracy, souls at 9/15 min."""
    ph = ",".join("?" * len(ids))
    ps = pd.read_sql_query(f"""SELECT match_id, player_slot, time_stamp_s, net_worth, kills, deaths, assists,
                                      player_damage, boss_damage, shots_hit, shots_missed
                               FROM player_stats WHERE match_id IN ({ph})""", conn, params=ids)
    if ps.empty:
        return pd.DataFrame(columns=["match_id", "player_slot"])
    key = ["match_id", "player_slot"]
    last = ps.sort_values("time_stamp_s").groupby(key).tail(1).set_index(key)
    tens = last["time_stamp_s"] / 600
    out = pd.DataFrame({
        "kills_p10": last["kills"] / tens, "deaths_p10": last["deaths"] / tens, "assists_p10": last["assists"] / tens,
        "kda": (last["kills"] + last["assists"]) / last["deaths"].clip(lower=1),
        "damage_per_min": last["player_damage"] / tens / 10, "objective_damage_per_min": last["boss_damage"] / tens / 10,
        "accuracy": last["shots_hit"] / (last["shots_hit"] + last["shots_missed"]).replace(0, np.nan),
    })
    for t, name in [(540, "souls_9m"), (900, "souls_15m")]:
        out[name] = ps[ps["time_stamp_s"] == t].set_index(key)["net_worth"]
    return out.reset_index()
