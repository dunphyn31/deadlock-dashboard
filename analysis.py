"""Load stored matches into DataFrames and derive per-minute game state for a tracked player."""
import numpy as np
import pandas as pd

RANK_NAMES = ["Obscurus", "Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic", "Ritualist",
              "Emissary", "Oracle", "Phantom", "Ascendant", "Eternus"]

OBJECTIVE_KINDS = {
    "Tier1": "Guardian", "Tier2": "Walker", "BarrackBoss": "Base Guardian",
    "TitanShieldGenerator": "Shrine", "Titan": "Patron", "Core": "Core",
}

# Cumulative per-player stats interpolated onto a per-minute grid.
GRID_COLS = ["net_worth", "level", "kills", "deaths", "assists", "denies", "creep_kills", "neutral_kills",
             "player_damage", "player_damage_taken", "player_healing", "boss_damage", "shots_hit",
             "shots_missed", "hero_bullets_hit", "hero_bullets_hit_crit", "ability_points"]


def badge_name(b):
    if b is None or pd.isna(b) or b <= 0:
        return "Unranked"
    tier, sub = divmod(int(b), 10)
    name = RANK_NAMES[tier] if tier < len(RANK_NAMES) else f"Tier {tier}"
    return f"{name} {sub}" if sub else name


# Objective lane numbers (assumed same left-to-right order as player lanes 1/4/6).
OBJECTIVE_LANES = {"1": "Yellow", "3": "Blue", "4": "Green"}


def objective_label(key):
    for prefix, label in OBJECTIVE_KINDS.items():
        if key.startswith(prefix):
            lane = "".join(ch for ch in key[len(prefix):] if ch.isdigit())
            if prefix == "TitanShieldGenerator" or not lane:
                return label
            return f"{OBJECTIVE_LANES.get(lane, 'Lane ' + lane)} {label}"
    return key


def load(conn, account_id, count=200):
    """All data for the tracked account's latest `count` ranked matches."""
    q = lambda sql, *p: pd.read_sql_query(sql, conn, params=p)
    hist = q("""
        SELECT h.*, he.name AS hero, m.average_badge, m.average_badge_team0, m.average_badge_team1,
               m.match_id IS NOT NULL AS has_meta
        FROM history h
        LEFT JOIN heroes he ON he.id = h.hero_id
        LEFT JOIN matches m ON m.match_id = h.match_id
        WHERE h.account_id = ?
        ORDER BY h.start_time DESC LIMIT ?""", account_id, count)
    hist["date"] = pd.to_datetime(hist["start_time"], unit="s", utc=True).dt.tz_convert(None)
    hist["result"] = np.where(hist["won"] == 1, "Win", "Loss")
    hist["kda"] = (hist["player_kills"] + hist["player_assists"]) / hist["player_deaths"].clip(lower=1)
    hist["duration_min"] = hist["match_duration_s"] / 60
    hist["rank"] = hist["ranked_display_badge"].map(badge_name)
    hist["lobby_rank"] = hist["average_badge"].map(badge_name)

    ids = tuple(int(x) for x in hist.loc[hist["has_meta"] == 1, "match_id"]) or (-1,)
    ph = ",".join("?" * len(ids))
    mp = q(f"""SELECT mp.*, he.name AS hero FROM match_players mp LEFT JOIN heroes he ON he.id = mp.hero_id
               WHERE match_id IN ({ph})""", *ids)
    ps = q(f"SELECT * FROM player_stats WHERE match_id IN ({ph})", *ids)
    obj = q(f"SELECT * FROM objectives WHERE match_id IN ({ph}) AND destroyed_time_s > 0", *ids)
    obj["label"] = obj["objective"].map(objective_label)
    boss = q(f"SELECT * FROM mid_boss WHERE match_id IN ({ph})", *ids)
    deaths = q(f"SELECT * FROM deaths WHERE match_id IN ({ph})", *ids)
    items = q(f"""SELECT i.*, r.name AS item, r.slot, r.tier, r.cost, r.type FROM items i
                  LEFT JOIN item_ref r ON r.id = i.item_id WHERE i.match_id IN ({ph})""", *ids)
    durations = hist.set_index("match_id")["match_duration_s"]

    me = mp[mp["account_id"] == account_id][["match_id", "player_slot", "team", "assigned_lane"]]
    me = me.rename(columns={"player_slot": "my_slot", "team": "my_team", "assigned_lane": "my_lane"})
    return dict(hist=hist, mp=mp, ps=ps, obj=obj, boss=boss, deaths=deaths, items=items,
                me=me, durations=durations, grid=build_grid(ps, mp, durations))


def build_grid(ps, mp, durations, step=60):
    """Linearly interpolate every player's cumulative stats onto a fixed time grid (0..match end)."""
    if ps.empty:
        return pd.DataFrame(columns=["match_id", "player_slot", "t", *GRID_COLS])
    frames = []
    for (mid, slot), g in ps.groupby(["match_id", "player_slot"], sort=False):
        g = g.sort_values("time_stamp_s")
        end = int(durations.get(mid, g["time_stamp_s"].max()))
        t = np.arange(0, end + 1, step)
        xs = np.concatenate([[0], g["time_stamp_s"].to_numpy()])
        out = {"match_id": mid, "player_slot": slot, "t": t}
        for c in GRID_COLS:
            ys = np.concatenate([[0], g[c].fillna(0).to_numpy()])
            out[c] = np.interp(t, xs, ys)
        frames.append(pd.DataFrame(out))
    grid = pd.concat(frames, ignore_index=True)
    return grid.merge(mp[["match_id", "player_slot", "team", "assigned_lane", "account_id", "hero"]],
                      on=["match_id", "player_slot"], how="left")


def team_curves(d):
    """Per match & minute: tracked player's values plus team totals and soul lead."""
    grid, me = d["grid"], d["me"]
    if grid.empty or me.empty:
        return pd.DataFrame()
    g = grid.merge(me, on="match_id")
    g["ally"] = g["team"] == g["my_team"]
    team = (g.groupby(["match_id", "t", "ally"])[["net_worth", "kills", "player_damage"]].sum()
            .unstack("ally"))
    team.columns = [f"{'team' if a else 'enemy'}_{c}" for c, a in team.columns]
    team = team.reset_index()
    mine = g[g["player_slot"] == g["my_slot"]].drop(columns=["team", "assigned_lane", "account_id", "hero", "ally"])

    # Lane opponents: enemies sharing the player's assigned lane.
    opp = (g[(~g["ally"]) & (g["assigned_lane"] == g["my_lane"])]
           .groupby(["match_id", "t"])["net_worth"].mean().rename("lane_opp_net_worth").reset_index())
    rank = g.copy()
    rank["nw_rank"] = rank.groupby(["match_id", "t"])["net_worth"].rank(ascending=False, method="min")
    rank = rank[rank["player_slot"] == rank["my_slot"]][["match_id", "t", "nw_rank"]]

    out = (mine.merge(team, on=["match_id", "t"]).merge(opp, on=["match_id", "t"], how="left")
           .merge(rank, on=["match_id", "t"]))
    out["soul_lead"] = out["team_net_worth"] - out["enemy_net_worth"]
    out["lane_diff"] = out["net_worth"] - out["lane_opp_net_worth"]
    out["nw_share"] = out["net_worth"] / out["team_net_worth"].replace(0, np.nan)
    won = d["hist"].set_index("match_id")["won"]
    out["won"] = out["match_id"].map(won)
    return out


def state_at(d, curves, minute):
    """One row per match: the game state for the tracked player at `minute` (matches still running)."""
    t = minute * 60
    s = curves[curves["t"] == t].copy()
    if s.empty:
        return s
    me = d["me"].set_index("match_id")
    s["my_team"] = s["match_id"].map(me["my_team"])

    obj = d["obj"][d["obj"]["destroyed_time_s"] <= t].merge(me.reset_index()[["match_id", "my_team"]])
    # `team` on an objective is the side that owned it, so an enemy-owned objective is one we took.
    taken = obj[obj["team"] != obj["my_team"]].groupby("match_id").size()
    lost = obj[obj["team"] == obj["my_team"]].groupby("match_id").size()
    s["objectives_taken"] = s["match_id"].map(taken).fillna(0).astype(int)
    s["objectives_lost"] = s["match_id"].map(lost).fillna(0).astype(int)

    boss = d["boss"][d["boss"]["destroyed_time_s"] <= t].merge(me.reset_index()[["match_id", "my_team"]])
    s["mid_boss_taken"] = s["match_id"].map(boss[boss["team_claimed"] == boss["my_team"]]
                                            .groupby("match_id").size()).fillna(0).astype(int)
    s["mid_boss_lost"] = s["match_id"].map(boss[boss["team_claimed"] != boss["my_team"]]
                                           .groupby("match_id").size()).fillna(0).astype(int)

    h = d["hist"].set_index("match_id")
    s["hero"] = s["match_id"].map(h["hero"])
    s["date"] = s["match_id"].map(h["date"])
    s["result"] = np.where(s["won"] == 1, "Win", "Loss")
    for c in ["kills", "deaths", "assists", "net_worth", "player_damage", "soul_lead", "lane_diff"]:
        s[c] = s[c].round()
    return s


def bucket_winrate(s, col, edges, labels=None):
    """Win rate and sample size per bucket of `col`."""
    b = pd.cut(s[col], edges, labels=labels)
    g = s.groupby(b, observed=False)["won"].agg(["mean", "size"]).reset_index()
    g.columns = [col, "win_rate", "games"]
    g[col] = g[col].astype(str)
    return g
