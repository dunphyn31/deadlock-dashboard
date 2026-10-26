"""Deadlock ranked-match dashboard.  Run:  streamlit run app.py"""
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import analysis as an
import behavior as bh
import db
import fetch

st.set_page_config(page_title="Deadlock Ranked Dashboard", page_icon="🎯", layout="wide")
WIN, LOSS = "#2e9d6a", "#d0544b"
RESULT_COLORS = {"Win": WIN, "Loss": LOSS}


def fmt_time(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


@st.cache_data(show_spinner="Loading matches ...")
def load_data(account_id, count, db_mtime):
    conn = db.connect()
    try:
        d = an.load(conn, account_id, count)
    finally:
        conn.close()
    d["curves"] = an.team_curves(d)
    return d


@st.cache_data(show_spinner="Classifying deaths and kills ...")
def behaviour_data(account_id, own_ids, params, db_mtime):
    conn = db.connect()
    try:
        base = [r[0] for r in conn.execute("SELECT match_id FROM baseline WHERE account_id = ?", (account_id,))]
        ids = [*own_ids, *base]
        deaths, kps, have = bh.analyze(conn, ids, dict(params))
        econ, snaps = bh.economy(conn, ids)
        pm = bh.player_matches(conn, account_id, own_ids, base, deaths, kps, econ)
    finally:
        conn.close()
    pm["has_paths"] = pm["match_id"].isin(have)
    return deaths, kps, econ, snaps, pm


def group_stats(pm, means, ratios):
    """By comparison group: per-match means of `means` and pooled ratios {name: (numerator, denominator)}."""
    g = pm.groupby("group")
    out = g[list(means)].mean()
    for name, (num, den) in ratios.items():
        out[name] = g[num].sum() / g[den].sum().replace(0, np.nan)
    out["player-matches"] = g.size()
    return out.reindex(GROUPS)


def compare_table(stats, labels):
    """Rows = metrics (labelled), columns = groups, plus the relative gap between you and the top players."""
    t = stats[list(labels)].T
    t.index = [labels[m] for m in t.index]
    t[f"You vs {TOP_SHORT}"] = t["You"] / t[TOP] - 1
    return t


def kpi(col, label, stats, metric, fmt, lower_is_better=True):
    you, top = stats.at["You", metric], stats.at[TOP, metric]
    delta = None if pd.isna(top) or pd.isna(you) else you - top
    col.metric(label, fmt.format(you) if pd.notna(you) else "–",
               None if delta is None else f"{fmt.format(delta)} vs {TOP_SHORT}",
               delta_color="inverse" if lower_is_better else "normal",
               help=f"{TOP}: {fmt.format(top)}" if pd.notna(top) else None)


def accounts():
    conn = db.connect()
    try:
        return pd.read_sql_query("SELECT * FROM accounts ORDER BY last_fetched DESC", conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------- sidebar
# Containers fix the visual order; they are filled below in whatever order the data allows.
sb_player, sb_filters, sb_compare, sb_rules, sb_data = (st.sidebar.container() for _ in range(5))
acc = accounts()
options = {f"{r.persona_name or 'Unknown'} ({r.account_id})": int(r.account_id) for r in acc.itertuples()}
with sb_player:
    st.header("Player")
    chosen = st.selectbox("Tracked player", list(options), index=0 if options else None,
                          placeholder="Add a player under Data", label_visibility="collapsed")
    account_id = options.get(chosen)

with sb_data:
    st.divider()
    with st.expander("Data", expanded=account_id is None):
        count = st.number_input("Ranked games to keep", 10, 500, 200, step=10)
        with st.form("fetch", border=False):
            new_player = st.text_input("Player", value=str(account_id) if account_id else "",
                                       help="SteamID3, SteamID64 or steamcommunity.com/profiles/… URL")
            go_fetch = st.form_submit_button("Fetch latest matches", width="stretch", type="primary")
        refresh_top = account_id is not None and st.button(
            "Refresh leaderboard comparison", width="stretch",
            help="Re-reads the NA and EU leaderboards (top 200) and fetches ~300 of those players' ranked "
                 "matches from the same period as yours. Replaces the current comparison set.")
    if go_fetch and new_player:
        try:
            aid = fetch.parse_account_id(new_player)
            with st.status(f"Fetching {aid} …", expanded=True) as status:
                ranked, stored, failed = fetch.update(aid, int(count), log=status.write)
                status.update(label=f"{ranked} ranked matches · {stored} new · {len(failed)} without metadata",
                              state="complete")
            st.cache_data.clear()
            st.rerun()
        except Exception as e:  # surface API/parse errors in the UI
            st.error(str(e))
    if refresh_top:
        with st.status("Fetching leaderboard players' matches …", expanded=True) as status:
            n = fetch.fetch_baseline(account_id, 300, log=status.write)
            status.update(label=f"{n} comparison matches", state="complete")
        st.cache_data.clear()
        st.rerun()

if account_id is None:
    st.title("Deadlock Ranked Dashboard")
    st.info("Open **Data** in the sidebar, enter a Steam ID and press **Fetch latest matches**.")
    st.stop()

d = load_data(account_id, int(count), db.DB_PATH.stat().st_mtime)
hist_all = d["hist"]
if hist_all.empty:
    st.warning("No ranked matches stored for this player yet — fetch them under **Data** in the sidebar.")
    st.stop()

with sb_filters:
    st.header("Filters")
    heroes = st.multiselect("Heroes", sorted(hist_all["hero"].dropna().unique()), placeholder="All heroes")
    dmin, dmax = hist_all["date"].min().date(), hist_all["date"].max().date()
    dates = st.date_input("Date range", (dmin, dmax), min_value=dmin, max_value=dmax)
    lobby = st.slider("Lobby average badge", 0, 116, (0, 116),
                      help="Applies to your matches and to the leaderboard comparison. "
                           "101 = Ascendant 1, 111 = Eternus 1.")
    st.caption(f"{an.badge_name(lobby[0]) if lobby[0] else 'Any'} – {an.badge_name(lobby[1])} lobbies")

with sb_compare:
    st.header("Compare with")
    top_n = st.select_slider("Leaderboard ranks (NA + EU)", [10, 25, 50, 100, 200], 200,
                             format_func=lambda n: f"top {n}")
    same_heroes = st.checkbox("Same heroes as me only",
                              help="Restrict teammates / enemies / top players to the heroes in your selection")

with sb_rules:
    with st.expander("Death & kill rules"):
        p_radius = st.slider("'Nearby' radius (units)", 500, 4000, 1500, 100,
                             help="A teammate within this distance counts as nearby. ~40 units ≈ 1 m.")
        p_window = st.slider("Caught out: no teammate nearby for the last (s)", 15, 30, 20)
        p_margin = st.slider("Enemy side: past midline by (units)", 0, 4000, 500, 100)
        p_before = st.slider("Overextended: no objective taken in the (s) before death", 0, 90, 30, 5)
        p_after = st.slider("… or the (s) after death", 0, 60, 15, 5)
        p_impact = st.slider("Low-impact kill: no objective within (s) after", 30, 60, 45, 5)
        p_assist = st.slider("Kill participation radius (units)", 500, 4000, 1500, 100,
                             help="Teammates this close to the victim when they died count as participating "
                                  "(the API only records the killer, not assists).")
        p_boss = st.checkbox("Count mid boss as an objective", True)
    params = tuple(sorted(dict(radius=p_radius, caught_window=p_window, side_margin=p_margin,
                               obj_before=p_before, obj_after=p_after, impact_window=p_impact,
                               assist_radius=p_assist, with_boss=p_boss).items()))

hist = hist_all
if heroes:
    hist = hist[hist["hero"].isin(heroes)]
if isinstance(dates, tuple) and len(dates) == 2:
    hist = hist[(hist["date"].dt.date >= dates[0]) & (hist["date"].dt.date <= dates[1])]
in_lobby = lambda b: b.between(*lobby) | (b.isna() & (lobby == (0, 116)))
match_ids_any_lobby = set(hist["match_id"])  # for the Lobby tiers tab, which splits by lobby itself
hist = hist[in_lobby(hist["average_badge"])]
match_ids = set(hist["match_id"])
TOP = f"Top {top_n} NA/EU"
TOP_SHORT = f"top {top_n}"
GROUPS = ["You", "Teammates", "Enemies", TOP]
curves = d["curves"]
curves = curves[curves["match_id"].isin(match_ids)] if not curves.empty else curves

deaths_all, kps_all, econ_all, snaps_all, pm_all = behaviour_data(
    account_id, tuple(sorted(int(m) for m in hist_all.loc[hist_all["has_meta"] == 1, "match_id"])), params,
    db.DB_PATH.stat().st_mtime)
pm_all = pm_all.assign(group=pm_all["group"].replace(bh.TOP, TOP))
top_rows = (pm_all["group"] == TOP) & (pm_all["lb_rank"] <= top_n)
# Sidebar filters: your matches (and the teammates/enemies in them) by every filter; leaderboard rows by
# lobby badge and leaderboard rank.
pm = pm_all[((top_rows & in_lobby(pm_all["average_badge"])) | pm_all["match_id"].isin(match_ids))
            & pm_all["has_paths"]]
if same_heroes:
    pm = pm[(pm["group"] == "You") | pm["hero"].isin(set(hist["hero"]))]
key = ["match_id", "player_slot"]
deaths = deaths_all.merge(pm[[*key, "group", "hero"]], on=key) if not deaths_all.empty else deaths_all
kps = kps_all.merge(pm[[*key, "group", "hero"]], on=key) if not kps_all.empty else kps_all

row = acc[acc["account_id"] == account_id].iloc[0]
c1, c2 = st.columns([1, 14], vertical_alignment="center")
if row["avatar"]:
    c1.image(row["avatar"], width=72)
c2.title(row["persona_name"] or str(account_id))
if hist.empty:
    st.warning("No matches match the current filters.")
    st.stop()
c2.caption(" · ".join([
    f"{len(hist)} ranked matches" + (f" of {len(hist_all)}" if len(hist) != len(hist_all) else ""),
    f"{hist['date'].min():%d %b %Y} – {hist['date'].max():%d %b %Y}",
    f"compared with {int((pm['group'] == TOP).sum())} games by {TOP} players",
]))

tab_over, tab_goals, tab_matches, tab_deaths, tab_econ, tab_tiers, tab_state, tab_items = st.tabs(
    ["Overview", "Goals", "Matches", "Deaths & fights", "Economy", "Lobby tiers", "Game state", "Items"])

# --------------------------------------------------------------------------- overview
with tab_over:
    k = st.columns(6)
    k[0].metric("Record", f"{int(hist['won'].sum())}–{int((1 - hist['won']).sum())}")
    k[1].metric("Win rate", f"{hist['won'].mean():.1%}")
    k[2].metric("KDA", f"{(hist['player_kills'].sum() + hist['player_assists'].sum()) / max(hist['player_deaths'].sum(), 1):.2f}")
    k[3].metric("Avg souls", f"{hist['net_worth'].mean():,.0f}")
    first_badge, last_badge_ = hist.iloc[-1]["ranked_display_badge"], hist.iloc[0]["ranked_display_badge"]
    k[4].metric("Current rank", hist.iloc[0]["rank"])
    k[5].metric("Rank change", f"{an.badge_name(first_badge)} → {an.badge_name(last_badge_)}",
                f"{int(last_badge_ - first_badge):+d} badge steps" if pd.notna(first_badge) else None,
                help="Rank badge at the first and last match in the selection")

    chron = hist.sort_values("start_time").reset_index(drop=True)
    chron["game"] = np.arange(1, len(chron) + 1)
    chron["rolling_wr"] = chron["won"].rolling(20, min_periods=5).mean()
    left, right = st.columns(2)
    fig = px.line(chron, x="date", y="ranked_display_badge", markers=True, title="Rank badge over time",
                  hover_data={"rank": True, "hero": True, "result": True, "ranked_display_badge": False})
    ticks = sorted(chron["ranked_display_badge"].dropna().unique())
    fig.update_yaxes(tickvals=ticks, ticktext=[an.badge_name(t) for t in ticks], title=None)
    left.plotly_chart(fig, width="stretch")
    fig = px.line(chron, x="game", y="rolling_wr", title="Rolling win rate (20 games)")
    fig.add_hline(0.5, line_dash="dot", line_color="gray")
    fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None)
    right.plotly_chart(fig, width="stretch")

    hero = (hist.groupby("hero").agg(games=("won", "size"), win_rate=("won", "mean"),
                                    kills=("player_kills", "mean"), deaths=("player_deaths", "mean"),
                                    assists=("player_assists", "mean"), souls=("net_worth", "mean"),
                                    last_hits=("last_hits", "mean"), minutes=("duration_min", "mean"))
            .sort_values("games", ascending=False).reset_index())
    left, right = st.columns([3, 2])
    left.subheader("Heroes")
    left.dataframe(hero, hide_index=True, width="stretch", column_config={
        "win_rate": st.column_config.ProgressColumn("Win rate", format="percent", min_value=0, max_value=1),
        **{c: st.column_config.NumberColumn(format="%.1f") for c in ["kills", "deaths", "assists", "minutes"]},
        "souls": st.column_config.NumberColumn(format="%d"), "last_hits": st.column_config.NumberColumn(format="%d"),
    })
    dur = an.bucket_winrate(hist.assign(won=hist["won"]), "duration_min", [0, 25, 30, 35, 40, 45, 200],
                            ["<25", "25–30", "30–35", "35–40", "40–45", "45+"])
    fig = px.bar(dur, x="duration_min", y="win_rate", text="games", title="Win rate by match length (min)")
    fig.update_traces(texttemplate="%{text} games", marker_color="#5b7bd5")
    fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None)
    fig.update_xaxes(title=None)
    right.plotly_chart(fig, width="stretch")

# --------------------------------------------------------------------------- goals
# (label, column, format, higher is better, what to do about it)
GOALS = [
    ("Shooting accuracy", "accuracy", "{:.1%}", True,
     "Aim practice, and take fights inside your weapon's falloff range rather than poking from max range."),
    ("Souls at 15:00", "souls_15m", "{:,.0f}", True,
     "Never leave a wave unpushed before rotating; clear a camp on the way to every fight."),
    ("Neutral souls / match", "souls_neutrals", "{:,.0f}", True,
     "Clear the nearest camp between waves and invade enemy camps when their laner is missing."),
    ("Last hits / possible", "last_hit_rate", "{:.1%}", True,
     "Last-hit every trooper in lane, including the ones you'd leave to your laning partner."),
    ("Kills / 10 min", "kills_p10", "{:.2f}", True,
     "Show up to fights earlier and commit to picks on enemies who are out of position."),
    ("Damage / min", "damage_per_min", "{:,.0f}", True,
     "Spend more of each fight shooting heroes instead of repositioning; follow up on your team's engages."),
    ("Deaths / 10 min", "deaths_p10", "{:.2f}", False,
     "Back off at half health and track enemies you can't see. Your win rate falls quickly as deaths rise."),
    ("Caught-out deaths / match", "caught_out", "{:.2f}", False,
     "Don't farm alone on the far side of the map when enemies are missing from the minimap."),
]


def hero_matched(you_df, top_df, col, min_games=5):
    """Top-player mean of `col`, weighted to the player's hero mix (heroes with enough top-player games)."""
    counts = you_df["hero"].value_counts()
    top_hero = top_df.groupby("hero")[col].agg(["mean", "size"])
    ok = [h for h in counts.index if h in top_hero.index and top_hero.at[h, "size"] >= min_games]
    if counts[ok].sum() < 0.5 * len(you_df):
        return top_df[col].mean(), False
    return (top_hero.loc[ok, "mean"] * counts[ok]).sum() / counts[ok].sum(), True


with tab_goals:
    you_g = pm[pm["group"] == "You"].merge(hist[["match_id", "start_time"]]).sort_values("start_time")
    top_g = pm[pm["group"] == TOP]
    if you_g.empty or top_g.empty:
        st.info("Goals need your match detail and the leaderboard comparison — fetch them under **Data**.")
    else:
        recent_n = st.select_slider("Current form = your last", [10, 20, 30, 50], 20, format_func=lambda n: f"{n} games")
        st.caption(f"Targets are the average of **{TOP}** players in the same lobby range, weighted to the heroes "
                   f"you play where there's enough data. The win rates show how your own games went when you "
                   f"hit the target and when you missed it.")
        cards = []
        for label, col, fmt, higher, action in GOALS:
            vals = you_g[["start_time", col, "won"]].dropna()
            if vals.empty:
                continue
            target, matched = hero_matched(you_g, top_g, col)
            if pd.isna(target):
                continue
            met = vals[col] >= target if higher else vals[col] <= target
            wr_met, wr_miss = vals.loc[met, "won"].mean(), vals.loc[~met, "won"].mean()
            overall, recent = vals[col].mean(), vals[col].tail(recent_n).mean()
            behind = overall < target if higher else overall > target
            cards.append(dict(label=label, col=col, fmt=fmt, higher=higher, action=action, target=target,
                              matched=matched, overall=overall, recent=recent, behind=behind, vals=vals,
                              met_share=met.mean(), wr_met=wr_met, wr_miss=wr_miss, n_met=int(met.sum()),
                              n_miss=int((~met).sum()), swing=(wr_met - wr_miss) if met.any() and (~met).any() else 0))
        # Work on: behind the target, ordered by how much hitting it has mattered in your own games.
        todo = sorted([c for c in cards if c["behind"]], key=lambda c: -c["swing"])
        keep = [c for c in cards if not c["behind"]]

        def card(c, box):
            with box.container(border=True):
                head, spark = st.columns([3, 2])
                delta = c["recent"] - c["target"]
                head.metric(c["label"], c["fmt"].format(c["recent"]),
                            f"{c['fmt'].format(delta)} vs target", delta_color="normal" if c["higher"] else "inverse",
                            help=f"Last {recent_n} games. All selected games: {c['fmt'].format(c['overall'])}")
                head.caption(f"Target **{c['fmt'].format(c['target'])}** ({TOP_SHORT}"
                             f"{', hero-matched' if c['matched'] else ''})")
                v = c["vals"].assign(roll=c["vals"][c["col"]].rolling(10, min_periods=3).mean())
                fig = go.Figure(go.Scatter(y=v["roll"], mode="lines", line=dict(color="#f2a900", width=2),
                                           hovertemplate="%{y:.3~s}<extra>rolling 10</extra>"))
                fig.add_hline(c["target"], line_dash="dot", line_color="#7a5bd5")
                fig.update_layout(height=110, margin=dict(l=0, r=0, t=6, b=0), showlegend=False,
                                  xaxis=dict(visible=False), yaxis=dict(visible=False))
                spark.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"spark_{c['col']}")
                if c["n_met"] and c["n_miss"]:
                    st.caption(f"Hit in {c['met_share']:.0%} of your games · win rate **{c['wr_met']:.0%}** when hit "
                               f"({c['n_met']}) vs **{c['wr_miss']:.0%}** when missed ({c['n_miss']})")
                if c["behind"] and c["swing"] <= 0.03:
                    st.caption(":gray-background[Low priority] — hitting this hasn't gone with more wins for you.")
                else:
                    st.markdown(f"**Do:** {c['action']}")

        st.subheader(f"Work on ({len(todo)})")
        cols = st.columns(2)
        for i, c in enumerate(todo):
            card(c, cols[i % 2])
        if keep:
            st.subheader(f"Keep doing ({len(keep)})")
            cols = st.columns(2)
            for i, c in enumerate(keep):
                card(c, cols[i % 2])

        st.subheader("Hero pool")
        hp = you_g.groupby("hero")["won"].agg(["mean", "size"])
        core = hp[(hp["size"] >= 8) & (hp["mean"] >= 0.55)].sort_values("size", ascending=False)
        weak = hp[(hp["size"] >= 6) & (hp["mean"] < 0.45)].sort_values("size", ascending=False)
        left, right = st.columns(2)
        with left.container(border=True):
            st.markdown("**Queue ranked on**")
            st.markdown("\n".join(f"- {h}: {r['mean']:.0%} over {int(r['size'])} games" for h, r in core.iterrows())
                        or "Not enough games on any hero yet.")
        with right.container(border=True):
            st.markdown("**Practise outside ranked**")
            st.markdown("\n".join(f"- {h}: {r['mean']:.0%} over {int(r['size'])} games" for h, r in weak.iterrows())
                        or "No hero below 45% with 6+ games.")
        st.caption("These patterns show what goes with your wins and losses, not proof of what causes them. "
                   "Small samples are noisy.")

# --------------------------------------------------------------------------- matches
with tab_matches:
    table = hist[["match_id", "date", "hero", "result", "player_kills", "player_deaths", "player_assists",
                  "net_worth", "last_hits", "denies", "duration_min", "rank", "ranked_delta", "lobby_rank",
                  "has_meta"]].reset_index(drop=True)
    st.caption("Select a row to open the match.")
    ev = st.dataframe(
        table, hide_index=True, width="stretch", height=420, on_select="rerun",
        selection_mode="single-row", key="match_table",
        column_config={
            "match_id": st.column_config.NumberColumn("Match", format="%d"),
            "date": st.column_config.DatetimeColumn("Date", format="YYYY-MM-DD HH:mm"),
            "player_kills": "K", "player_deaths": "D", "player_assists": "A",
            "net_worth": st.column_config.NumberColumn("Souls", format="%d"),
            "last_hits": "LH", "denies": "Denies",
            "duration_min": st.column_config.NumberColumn("Min", format="%.1f"),
            "rank": "Rank", "ranked_delta": st.column_config.NumberColumn("Δ rank", format="%+d"),
            "lobby_rank": "Lobby avg", "has_meta": st.column_config.CheckboxColumn("Metadata"),
        })
    picks = [f"{r.date:%Y-%m-%d %H:%M} · {r.hero} · {r.result} · {r.match_id}" for r in table.itertuples()]
    sel = ev.selection.rows
    pick = st.selectbox("Open match", range(len(picks)), index=sel[0] if sel else 0,
                        format_func=picks.__getitem__, key=f"pick_{sel[0] if sel else 'none'}")
    if pick is not None:
        m = table.iloc[pick]
        mid = int(m["match_id"])
        st.divider()
        st.subheader(f"{m['hero']} · {m['result']} · {m['date']:%Y-%m-%d %H:%M} · match {mid}")
        if not m["has_meta"]:
            st.info("The API has no metadata for this match, so only the summary above is available.")
        else:
            mp = d["mp"][d["mp"]["match_id"] == mid].copy()
            me = d["me"].set_index("match_id").loc[mid]
            mcur = d["grid"][d["grid"]["match_id"] == mid]
            dur_s = int(d["durations"][mid])

            minute = st.slider("Game state at minute", 0, dur_s // 60, min(15, dur_s // 60), key=f"min_{mid}")
            at = mcur[mcur["t"] == minute * 60].copy()
            at["side"] = np.where(at["team"] == me["my_team"], "Your team", "Enemy")
            at["you"] = at["player_slot"] == me["my_slot"]
            at["assigned_lane"] = at["assigned_lane"].map(bh.LANES)
            st.caption("Scoreboard at the selected minute (interpolated between stat snapshots)")
            cols = st.columns(2)
            for col, side in zip(cols, ["Your team", "Enemy"]):
                part = at[at["side"] == side].sort_values("net_worth", ascending=False)
                col.markdown(f"**{side}** — {part['net_worth'].sum():,.0f} souls")
                col.dataframe(
                    part[["hero", "you", "assigned_lane", "net_worth", "level", "kills", "deaths", "assists",
                          "creep_kills", "player_damage", "player_healing"]].round(0),
                    hide_index=True, width="stretch",
                    column_config={"you": st.column_config.CheckboxColumn("You"), "assigned_lane": "Lane",
                                   "net_worth": "Souls", "creep_kills": "Creeps", "player_damage": "Hero dmg",
                                   "player_healing": "Healing"})

            # Soul lead with objective / boss / death markers
            lead = curves[curves["match_id"] == mid] if not curves.empty else pd.DataFrame()
            fig = go.Figure()
            if not lead.empty:
                fig.add_scatter(x=lead["t"] / 60, y=lead["soul_lead"], mode="lines", name="Team soul lead",
                                line=dict(color="#5b7bd5", width=3), fill="tozeroy")
            o = d["obj"][d["obj"]["match_id"] == mid]
            for ours, name, color, sym in [(False, "Objective taken", WIN, "triangle-up"),
                                           (True, "Objective lost", LOSS, "triangle-down")]:
                oo = o[(o["team"] == me["my_team"]) == ours]
                fig.add_scatter(x=oo["destroyed_time_s"] / 60, y=[0] * len(oo), mode="markers", name=name,
                                marker=dict(color=color, size=12, symbol=sym), text=oo["label"],
                                hovertemplate="%{text} @ %{x:.1f} min<extra></extra>")
            b = d["boss"][d["boss"]["match_id"] == mid]
            for r in b.itertuples():
                fig.add_vline(r.destroyed_time_s / 60, line_dash="dash",
                              line_color=WIN if r.team_claimed == me["my_team"] else LOSS,
                              annotation_text="Mid boss")
            dd = d["deaths"][(d["deaths"]["match_id"] == mid) & (d["deaths"]["player_slot"] == me["my_slot"])]
            fig.add_scatter(x=dd["game_time_s"] / 60, y=[0] * len(dd), mode="markers", name="Your deaths",
                            marker=dict(color="black", size=8, symbol="x"),
                            hovertemplate="Died @ %{x:.1f} min<extra></extra>")
            fig.add_vline(minute, line_color="gray", line_width=1)
            fig.update_layout(title="Team soul lead, objectives and deaths", xaxis_title="Minute",
                              yaxis_title="Souls", hovermode="x unified", height=420)
            st.plotly_chart(fig, width="stretch")

            stat = st.selectbox("Compare stat across the lobby", an.GRID_COLS, key=f"stat_{mid}")
            pl = mcur.copy()
            pl["side"] = np.where(pl["player_slot"] == me["my_slot"], "You",
                                  np.where(pl["team"] == me["my_team"], "Ally", "Enemy"))
            fig = px.line(pl, x=pl["t"] / 60, y=stat, line_group="player_slot", color="side",
                          hover_name="hero", color_discrete_map={"You": "#f2a900", "Ally": "#8fb3e8", "Enemy": "#e8a0a0"})
            fig.update_traces(selector=dict(name="You"), line_width=4)
            fig.update_layout(xaxis_title="Minute", height=380)
            st.plotly_chart(fig, width="stretch")

            left, right = st.columns(2)
            it = d["items"][(d["items"]["match_id"] == mid) & (d["items"]["player_slot"] == me["my_slot"])
                            & (d["items"]["type"] == "upgrade")].sort_values("game_time_s")
            it = it.assign(bought=it["game_time_s"].map(fmt_time),
                           sold=it["sold_time_s"].map(lambda s: fmt_time(s) if s else ""))
            left.markdown("**Item build**")
            left.dataframe(it[["bought", "item", "slot", "tier", "cost", "sold"]], hide_index=True,
                           width="stretch", height=360)
            slots = mp.set_index("player_slot")["hero"]
            dd = dd.assign(time=dd["game_time_s"].map(fmt_time), killer=dd["killer_player_slot"].map(slots),
                           respawn_s=dd["death_duration_s"])
            if not deaths_all.empty:
                cat = deaths_all[deaths_all["match_id"] == mid][["player_slot", "t", "caught_out", "overextended",
                                                                "nearest_teammate"]]
                dd = dd.merge(cat, left_on=["player_slot", "game_time_s"], right_on=["player_slot", "t"], how="left")
            right.markdown(f"**Your deaths ({len(dd)})**")
            right.dataframe(dd[[c for c in ["time", "killer", "caught_out", "overextended", "nearest_teammate",
                                            "respawn_s", "time_to_kill_s"] if c in dd]].round(1),
                            hide_index=True, width="stretch", height=360,
                            column_config={"caught_out": st.column_config.CheckboxColumn("Caught out"),
                                           "overextended": st.column_config.CheckboxColumn("Overextended"),
                                           "nearest_teammate": st.column_config.NumberColumn(
                                               "Nearest ally (units)", format="%d")})

# --------------------------------------------------------------------------- game state across matches
with tab_state:
    if curves.empty:
        st.info("No matches with metadata in the current selection.")
    else:
        st.caption("How the game looked at a given minute, and how often that turned into a win. "
                   "Values are interpolated from the stat snapshots the API records (every 3 min early, "
                   "then every 5 min).")
        minute = st.slider("Minute", 3, 45, 15, key="state_minute")
        s = an.state_at(d, curves, minute)
        if s.empty:
            st.info("No selected matches lasted that long.")
        else:
            k = st.columns(5)
            k[0].metric("Matches reaching this minute", len(s))
            ahead, behind = s[s["soul_lead"] > 0], s[s["soul_lead"] <= 0]
            k[1].metric("Win rate when team ahead", f"{ahead['won'].mean():.0%}" if len(ahead) else "–",
                        help=f"{len(ahead)} games")
            k[2].metric("Win rate when team behind", f"{behind['won'].mean():.0%}" if len(behind) else "–",
                        help=f"{len(behind)} games")
            la, lb = s[s["lane_diff"] > 0], s[s["lane_diff"] <= 0]
            k[3].metric("Win rate when ahead of lane opp.", f"{la['won'].mean():.0%}" if len(la) else "–",
                        help=f"{len(la)} games")
            k[4].metric("Win rate when behind lane opp.", f"{lb['won'].mean():.0%}" if len(lb) else "–",
                        help=f"{len(lb)} games")

            def wr_bar(df, col, edges, labels, title):
                g = an.bucket_winrate(df, col, edges, labels)
                fig = px.bar(g, x=col, y="win_rate", text="games", title=title)
                fig.update_traces(texttemplate="%{text} games", marker_color="#5b7bd5")
                fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None)
                fig.update_xaxes(title=None)
                return fig

            inf = float("inf")
            left, right = st.columns(2)
            left.plotly_chart(wr_bar(s, "soul_lead", [-inf, -8000, -3000, -1000, 1000, 3000, 8000, inf],
                                     ["< -8k", "-8k to -3k", "-3k to -1k", "even", "+1k to +3k", "+3k to +8k", "> +8k"],
                                     "Win rate by team soul lead"), width="stretch")
            right.plotly_chart(wr_bar(s, "lane_diff", [-inf, -2000, -500, 500, 2000, inf],
                                      ["< -2k", "-2k to -500", "even", "+500 to +2k", "> +2k"],
                                      "Win rate by your souls vs lane opponents"), width="stretch")
            left, right = st.columns(2)
            fig = px.scatter(s, x="soul_lead", y="lane_diff", color="result", color_discrete_map=RESULT_COLORS,
                             hover_data=["hero", "date", "kills", "deaths", "assists", "objectives_taken",
                                         "objectives_lost"], title="Team lead vs your lane lead")
            fig.add_hline(0, line_color="gray", line_width=1)
            fig.add_vline(0, line_color="gray", line_width=1)
            left.plotly_chart(fig, width="stretch")
            right.plotly_chart(wr_bar(s, "nw_rank", [0, 2, 4, 6, 8, 10, 12],
                                      ["1–2", "3–4", "5–6", "7–8", "9–10", "11–12"],
                                      "Win rate by your souls rank in the lobby"), width="stretch")

            st.subheader("How games develop: wins vs losses")
            metric = st.selectbox("Metric", ["soul_lead", "lane_diff", "net_worth", "nw_share", "kills",
                                             "deaths", "player_damage", "nw_rank"], key="curve_metric")
            c = curves.assign(minute=curves["t"] / 60, result=np.where(curves["won"] == 1, "Win", "Loss"))
            c = c[c["minute"] <= 45]
            agg = (c.groupby(["result", "minute"])[metric]
                   .agg(median="median", q25=lambda x: x.quantile(.25), q75=lambda x: x.quantile(.75), n="size")
                   .reset_index())
            agg = agg[agg["n"] >= 5]
            fig = go.Figure()
            for res, color in RESULT_COLORS.items():
                a = agg[agg["result"] == res]
                fig.add_scatter(x=pd.concat([a["minute"], a["minute"][::-1]]),
                                y=pd.concat([a["q75"], a["q25"][::-1]]), fill="toself", line=dict(width=0),
                                fillcolor=color, opacity=0.18, showlegend=False, hoverinfo="skip")
                fig.add_scatter(x=a["minute"], y=a["median"], name=f"{res} (median)", line=dict(color=color, width=3))
            fig.add_vline(minute, line_color="gray", line_width=1)
            fig.update_layout(xaxis_title="Minute", yaxis_title=metric, height=420,
                              title="Median with interquartile band")
            st.plotly_chart(fig, width="stretch")

            st.subheader("Comebacks and throws")
            thr = st.number_input("Soul lead threshold", 500, 20000, 3000, step=500)
            left, right = st.columns(2)
            show = ["date", "hero", "soul_lead", "lane_diff", "kills", "deaths", "assists",
                    "objectives_taken", "objectives_lost", "match_id"]
            cb, th = s[(s["soul_lead"] <= -thr) & (s["won"] == 1)], s[(s["soul_lead"] >= thr) & (s["won"] == 0)]
            left.markdown(f"**Comebacks** — won after being ≥{thr:,} behind: {len(cb)} of "
                          f"{(s['soul_lead'] <= -thr).sum()}")
            left.dataframe(cb[show], hide_index=True, width="stretch")
            right.markdown(f"**Throws** — lost after being ≥{thr:,} ahead: {len(th)} of "
                           f"{(s['soul_lead'] >= thr).sum()}")
            right.dataframe(th[show], hide_index=True, width="stretch")

            with st.expander(f"All match states at {minute}:00"):
                st.dataframe(s.drop(columns=["t", "my_slot", "my_team", "my_lane"]), hide_index=True,
                             width="stretch")
                st.download_button("Download CSV", s.to_csv(index=False), f"state_{minute}min.csv", "text/csv")

# --------------------------------------------------------------------------- deaths & fights
GROUP_COLORS = {"You": "#f2a900", "Teammates": "#5b7bd5", "Enemies": "#d0544b", TOP: "#7a5bd5"}


@st.cache_data(show_spinner=False)
def map_background(match_ids, db_mtime, max_matches=25):
    """Sampled alive positions from a few matches, used as a faint map silhouette."""
    conn = db.connect()
    try:
        xs, ys = [], []
        for mid in list(match_ids)[:max_matches]:
            for a in bh.load_paths(conn, mid).values():
                a = a[:, ::6]
                alive = a[2] > 0
                xs.append(a[0, alive])
                ys.append(a[1, alive])
    finally:
        conn.close()
    return (np.concatenate(xs), np.concatenate(ys)) if xs else (np.array([]), np.array([]))


def phase(t):
    return pd.cut(t / 60, [0, 10, 20, 30, 999], labels=["0–10", "10–20", "20–30", "30+"], right=False)


with tab_deaths:
    if pm.empty or deaths.empty:
        st.info("No position data for the current selection. Re-run the fetch to download match detail.")
    else:
        if not (pm["group"] == TOP).any():
            st.warning("No leaderboard comparison data for these filters — refresh it under **Data**.")
        with st.expander("How deaths and kills are classified"):
            st.markdown(
                f"- **Caught out**: no living teammate within {p_radius:,} units at any point in the "
                f"{p_window}s before dying.\n"
                f"- **Overextended**: died more than {p_margin:,} units past the midline on the enemy half, with "
                f"no enemy objective{' or mid boss' if p_boss else ''} taken from {p_before}s before to "
                f"{p_after}s after.\n"
                f"- **Low-impact kill**: a kill you took part in (killer, or within {p_assist:,} units of the "
                f"victim) not followed by an objective within {p_impact}s.\n\n"
                "Change any of these under **Death & kill rules** in the sidebar.")

        stats = group_stats(
            pm, ["deaths", "caught_out", "overextended", "enemy_side_deaths", "time_dead_s",
                 "kill_participations", "low_impact_kp"],
            {"caught_share": ("caught_out", "deaths"), "over_share": ("overextended", "deaths"),
             "low_share": ("low_impact_kp", "kill_participations")})
        k = st.columns(6)
        kpi(k[0], "Deaths / match", stats, "deaths", "{:.1f}")
        kpi(k[1], "Caught out / match", stats, "caught_out", "{:.2f}")
        kpi(k[2], "% deaths caught out", stats, "caught_share", "{:.0%}")
        kpi(k[3], "Overextended / match", stats, "overextended", "{:.2f}")
        kpi(k[4], "% deaths overextended", stats, "over_share", "{:.0%}")
        kpi(k[5], "% kills low-impact", stats, "low_share", "{:.0%}")

        labels = {"deaths": "Deaths / match", "caught_out": "Caught-out deaths / match",
                  "caught_share": "Share of deaths caught out", "overextended": "Overextended deaths / match",
                  "over_share": "Share of deaths overextended", "enemy_side_deaths": "Deaths on enemy half / match",
                  "time_dead_s": "Time dead (s) / match", "kill_participations": "Kill participations / match",
                  "low_impact_kp": "Low-impact kill participations / match",
                  "low_share": "Share of kill participations low-impact", "player-matches": "Player-matches"}
        st.dataframe(compare_table(stats, labels), width="stretch", column_config={
            g: st.column_config.NumberColumn(format="%.2f") for g in GROUPS} | {
            f"You vs {TOP_SHORT}": st.column_config.NumberColumn(format="percent")})

        # Within-match comparison: you vs the average teammate in the same game.
        st.subheader("You vs your own teammates, game by game")
        mine = pm[pm["group"] == "You"].set_index("match_id")
        mates = pm[pm["group"] == "Teammates"].groupby("match_id")
        cmp_cols = {"caught_out": "Caught out", "overextended": "Overextended", "low_impact_kp": "Low-impact kills",
                    "deaths": "Deaths"}
        rows = []
        for c, lbl in cmp_cols.items():
            diff = (mine[c] - mates[c].mean()).dropna()
            worst = (mine[c] >= mates[c].max().reindex(mine.index)).reindex(diff.index)
            rows.append({"metric": lbl, "you (avg)": mine[c].mean(), "teammates (avg)": mates[c].mean().mean(),
                         "avg difference": diff.mean(), "games above team avg": (diff > 0).mean(),
                         "games highest on team": worst.mean()})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "games above team avg": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
            "games highest on team": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
            **{c: st.column_config.NumberColumn(format="%.2f") for c in ["you (avg)", "teammates (avg)",
                                                                         "avg difference"]}})

        my_d = deaths[deaths["group"] == "You"].copy()
        my_d["category"] = np.select(
            [my_d["caught_out"] & my_d["overextended"], my_d["caught_out"], my_d["overextended"]],
            ["Caught out + overextended", "Caught out", "Overextended"], "Other")
        cat_colors = {"Caught out + overextended": "#b0306a", "Caught out": "#e8862a", "Overextended": "#d0544b",
                      "Other": "#8a8f98"}
        left, right = st.columns([3, 2])
        bx, by = map_background(tuple(sorted(match_ids)), db.DB_PATH.stat().st_mtime)
        fig = go.Figure()
        fig.add_scattergl(x=bx, y=by, mode="markers", marker=dict(size=2, color="rgba(140,140,140,0.08)"),
                          hoverinfo="skip", showlegend=False)
        for cat_name, color in cat_colors.items():
            c = my_d[my_d["category"] == cat_name]
            fig.add_scattergl(x=c["x"], y=c["depth"], mode="markers", name=f"{cat_name} ({len(c)})",
                              marker=dict(size=7, color=color, line=dict(width=0.5, color="white")),
                              customdata=np.stack([c["t"].map(fmt_time), c["hero"],
                                                   c["nearest_teammate"].round()], axis=1) if len(c) else None,
                              hovertemplate="%{customdata[1]} @ %{customdata[0]}<br>nearest ally "
                                            "%{customdata[2]} units<extra></extra>")
        fig.add_hline(0, line_dash="dot", line_color="gray", annotation_text="midline")
        fig.update_layout(title="Your deaths (your base at the bottom)", height=620,
                          xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"),
                          legend=dict(orientation="h", y=-0.02))
        left.plotly_chart(fig, width="stretch")

        d_ph = deaths.assign(phase=phase(deaths["t"]))
        per = (d_ph.groupby(["group", "phase"], observed=False)[["caught_out", "overextended"]].sum()
               .div(pm.groupby("group").size(), level="group").reset_index())
        per = per.melt(["group", "phase"], var_name="kind", value_name="per_match")
        fig = px.bar(per, x="phase", y="per_match", color="group", facet_row="kind", barmode="group",
                     color_discrete_map=GROUP_COLORS, category_orders={"group": GROUPS},
                     title="Deaths per match by game phase (minutes)", height=620)
        fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1].replace("_", " ")))
        fig.update_yaxes(title=None)
        right.plotly_chart(fig, width="stretch")

        left, right = st.columns(2)
        trend = (mine.reset_index()[["match_id", "caught_out", "overextended", "low_impact_kp"]]
                 .merge(hist[["match_id", "start_time"]]).sort_values("start_time"))
        trend["game"] = np.arange(1, len(trend) + 1)
        for c in ["caught_out", "overextended", "low_impact_kp"]:
            trend[c] = trend[c].rolling(20, min_periods=5).mean()
        fig = px.line(trend.melt(["game"], ["caught_out", "overextended", "low_impact_kp"]), x="game", y="value",
                      color="variable", title="Your trend (rolling 20 games, per match)")
        for c, col in [("caught_out", "#e8862a"), ("overextended", "#d0544b")]:
            if pd.notna(stats.at[TOP, c]):
                fig.add_hline(stats.at[TOP, c], line_dash="dot", line_color=col,
                              annotation_text=f"{TOP_SHORT} {c.replace('_', ' ')}")
        fig.update_layout(yaxis_title=None, legend_title=None)
        left.plotly_chart(fig, width="stretch")

        hero_t = (pm[pm["group"] == "You"].groupby("hero")
                  .agg(games=("match_id", "size"), deaths=("deaths", "mean"), caught_out=("caught_out", "mean"),
                       overextended=("overextended", "mean"), low_impact_kp=("low_impact_kp", "mean"),
                       win_rate=("won", "mean"))
                  .sort_values("games", ascending=False).reset_index())
        top_hero = (pm[pm["group"] == TOP].groupby("hero")[["caught_out", "overextended"]].mean()
                    .add_prefix("top_").reset_index())
        hero_t = hero_t.merge(top_hero, on="hero", how="left")
        right.markdown("**By hero (per match)**")
        right.dataframe(hero_t, hide_index=True, width="stretch", height=380, column_config={
            "win_rate": st.column_config.ProgressColumn("Win rate", format="percent", min_value=0, max_value=1),
            **{c: st.column_config.NumberColumn(format="%.2f")
               for c in ["deaths", "caught_out", "overextended", "low_impact_kp", "top_caught_out",
                         "top_overextended"]}})

        st.subheader("Kill participation impact")
        horizon = np.arange(0, 181, 5)
        curve = []
        for g, part in kps.groupby("group"):
            nxt = part["next_objective_s"].to_numpy()
            curve += [{"group": g, "seconds": h, "converted": (nxt <= h).mean()} for h in horizon]
        left, right = st.columns(2)
        fig = px.line(pd.DataFrame(curve), x="seconds", y="converted", color="group",
                      color_discrete_map=GROUP_COLORS, category_orders={"group": GROUPS},
                      title="Share of kill participations followed by an objective within N seconds")
        fig.add_vrect(30, 60, fillcolor="gray", opacity=0.1, line_width=0)
        fig.add_vline(p_impact, line_dash="dot", line_color="gray")
        fig.update_yaxes(tickformat=".0%", title=None)
        left.plotly_chart(fig, width="stretch")
        lw = pm[pm["group"] == "You"].copy()
        lw = an.bucket_winrate(lw, "low_impact_share", [-0.01, 0.4, 0.55, 0.7, 0.85, 1.0],
                               ["<40%", "40–55%", "55–70%", "70–85%", "85%+"])
        fig = px.bar(lw, x="low_impact_share", y="win_rate", text="games",
                     title="Your win rate by share of low-impact kill participations")
        fig.update_traces(texttemplate="%{text} games", marker_color="#5b7bd5")
        fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None)
        fig.update_xaxes(title=None)
        right.plotly_chart(fig, width="stretch")

        with st.expander(f"Your deaths ({len(my_d)})"):
            cat_pick = st.multiselect("Category", list(cat_colors), ["Caught out", "Overextended",
                                                                       "Caught out + overextended"])
            log = my_d[my_d["category"].isin(cat_pick)].merge(hist[["match_id", "date", "result"]])
            log = log.assign(time=log["t"].map(fmt_time),
                             nearest_teammate=log["nearest_teammate"].replace(np.inf, np.nan))
            st.dataframe(log[["date", "hero", "result", "time", "category", "nearest_teammate", "depth",
                              "objective_nearby", "death_duration_s", "match_id"]].sort_values("date", ascending=False),
                         hide_index=True, width="stretch", column_config={
                             "match_id": st.column_config.NumberColumn(format="%d"),
                             "nearest_teammate": st.column_config.NumberColumn("Nearest ally", format="%d"),
                             "depth": st.column_config.NumberColumn("Past midline", format="%d"),
                             "death_duration_s": "Respawn (s)"})

# --------------------------------------------------------------------------- economy
with tab_econ:
    if pm.empty or "avg_unspent_souls" not in pm or pm["avg_unspent_souls"].isna().all():
        st.info("No economy detail for the current selection. Re-run the fetch to download match detail.")
    else:
        stats = group_stats(
            pm, ["souls_per_min", "avg_unspent_souls", "avg_unspent_ap", "souls_lost_on_death",
                 "souls_lost_per_death", "orb_secure_share", "last_hit_rate", "time_dead_s"], {})
        k = st.columns(6)
        kpi(k[0], "Souls / min", stats, "souls_per_min", "{:,.0f}", lower_is_better=False)
        kpi(k[1], "Avg unspent souls", stats, "avg_unspent_souls", "{:,.0f}")
        kpi(k[2], "Unsecured souls lost / match", stats, "souls_lost_on_death", "{:,.0f}")
        kpi(k[3], "Souls lost / death", stats, "souls_lost_per_death", "{:,.0f}")
        kpi(k[4], "Lane souls from secured orbs", stats, "orb_secure_share", "{:.0%}", lower_is_better=False)
        kpi(k[5], "Avg unspent AP", stats, "avg_unspent_ap", "{:.2f}")
        st.caption("Avg unspent souls = the game's 'Unspent Gold Minutes' stat (souls held × minutes) ÷ match "
                   "minutes. Souls lost = unsecured souls dropped on death (gold_death_loss).")

        labels = {"souls_per_min": "Souls / min", "avg_unspent_souls": "Avg unspent souls",
                  "avg_unspent_ap": "Avg unspent ability points", "souls_lost_on_death": "Unsecured souls lost / match",
                  "souls_lost_per_death": "Souls lost / death", "orb_secure_share": "Lane souls from secured orbs",
                  "last_hit_rate": "Last hits / possible creeps", "time_dead_s": "Time dead (s) / match",
                  "player-matches": "Player-matches"}
        st.dataframe(compare_table(stats, labels), width="stretch", column_config={
            g: st.column_config.NumberColumn(format="%.2f") for g in GROUPS} | {
            f"You vs {TOP_SHORT}": st.column_config.NumberColumn(format="percent")})

        sn = snaps_all.merge(pm[[*key, "group"]], on=key)
        sn = sn[sn["time_stamp_s"] % 60 == 0]  # drop the partial end-of-match sample
        left, right = st.columns(2)
        for col, metric, title in [(left, "unspent_avg", "Average unspent souls through the match"),
                                   (right, "unspent_ap_avg", "Average unspent ability points")]:
            cur = (sn.groupby(["group", "time_stamp_s"])[metric].agg(["mean", "size"]).reset_index())
            cur = cur[cur["size"] >= 10]
            cur["minute"] = cur["time_stamp_s"] / 60
            fig = px.line(cur, x="minute", y="mean", color="group", markers=True, title=title,
                          color_discrete_map=GROUP_COLORS, category_orders={"group": GROUPS})
            fig.update_layout(yaxis_title=None, xaxis_title="Minute (end of snapshot interval)")
            col.plotly_chart(fig, width="stretch")

        left, right = st.columns(2)
        fig = px.box(pm, x="group", y="souls_lost_on_death", color="group", points=False,
                     color_discrete_map=GROUP_COLORS, category_orders={"group": GROUPS},
                     title="Unsecured souls lost per match")
        fig.update_layout(showlegend=False, xaxis_title=None, yaxis_title=None)
        left.plotly_chart(fig, width="stretch")
        src = {"souls_lane": "Lane creeps", "souls_neutrals": "Neutrals", "souls_kills": "Hero kills",
               "souls_assists": "Assists", "souls_bosses": "Bosses", "souls_treasure": "Treasure/breakables",
               "souls_powerups": "Power-ups", "souls_comeback": "Comeback", "souls_denies": "Denies",
               "souls_team_bonus": "Team bonus"}
        inc = pm.groupby("group")[list(src)].mean().reindex(GROUPS).rename(columns=src)
        inc = inc.div(inc.sum(axis=1), axis=0).reset_index().melt("group", var_name="source", value_name="share")
        fig = px.bar(inc, x="share", y="group", color="source", orientation="h", title="Where souls come from",
                     category_orders={"group": GROUPS[::-1]})
        fig.update_xaxes(tickformat=".0%", title=None)
        fig.update_yaxes(title=None)
        right.plotly_chart(fig, width="stretch")

        st.subheader("Within your matches")
        lob = pm[pm["match_id"].isin(match_ids) & pm["group"].isin(["You", "Teammates", "Enemies"])].copy()
        for c in ["avg_unspent_souls", "souls_lost_on_death", "souls_per_min"]:
            lob[f"{c}_rank"] = lob.groupby("match_id")[c].rank(ascending=c == "souls_per_min", method="min")
        mine = lob[lob["group"] == "You"].merge(hist[["match_id", "date", "result"]])
        lobby_avg = lob[lob["group"] != "You"].groupby("match_id")[
            ["avg_unspent_souls", "souls_lost_on_death", "souls_per_min"]].mean().add_suffix("_lobby")
        mine = mine.merge(lobby_avg, left_on="match_id", right_index=True)
        k = st.columns(3)
        k[0].metric("Games you held the most unspent souls in the lobby",
                    f"{(mine['avg_unspent_souls_rank'] == 12).mean():.0%}",
                    help="Rank 12 = highest average unspent souls of all 12 players")
        k[1].metric("Games in the top 3 for souls lost on death", f"{(mine['souls_lost_on_death_rank'] >= 10).mean():.0%}")
        k[2].metric("Avg souls/min rank in lobby", f"{mine['souls_per_min_rank'].mean():.1f}",
                    help="1 = highest souls per minute in the match")
        left, right = st.columns(2)
        wr = an.bucket_winrate(mine, "avg_unspent_souls", [0, 1000, 1500, 2000, 2500, 3000, 1e9],
                               ["<1k", "1–1.5k", "1.5–2k", "2–2.5k", "2.5–3k", "3k+"])
        fig = px.bar(wr, x="avg_unspent_souls", y="win_rate", text="games",
                     title="Your win rate by average unspent souls")
        fig.update_traces(texttemplate="%{text} games", marker_color="#5b7bd5")
        fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None)
        fig.update_xaxes(title=None)
        left.plotly_chart(fig, width="stretch")
        wr = an.bucket_winrate(mine, "souls_lost_on_death", [-1, 1000, 2000, 3000, 5000, 1e9],
                               ["<1k", "1–2k", "2–3k", "3–5k", "5k+"])
        fig = px.bar(wr, x="souls_lost_on_death", y="win_rate", text="games",
                     title="Your win rate by unsecured souls lost")
        fig.update_traces(texttemplate="%{text} games", marker_color="#5b7bd5")
        fig.update_yaxes(tickformat=".0%", range=[0, 1], title=None)
        fig.update_xaxes(title=None)
        right.plotly_chart(fig, width="stretch")
        with st.expander("Per match"):
            st.dataframe(mine[["date", "hero", "result", "souls_per_min", "souls_per_min_lobby", "avg_unspent_souls",
                               "avg_unspent_souls_lobby", "souls_lost_on_death", "souls_lost_on_death_lobby",
                               "orb_secure_share", "avg_unspent_ap", "match_id"]].sort_values("date", ascending=False),
                         hide_index=True, width="stretch", column_config={
                             "match_id": st.column_config.NumberColumn(format="%d"),
                             "orb_secure_share": st.column_config.NumberColumn("Orb share", format="percent"),
                             **{c: st.column_config.NumberColumn(format="%d") for c in [
                                 "souls_per_min", "souls_per_min_lobby", "avg_unspent_souls", "avg_unspent_souls_lobby",
                                 "souls_lost_on_death", "souls_lost_on_death_lobby"]}})

# --------------------------------------------------------------------------- lobby tiers
# (label, column, number format, higher is better)
TIER_METRICS = [
    ("Win rate", "won", "{:.0%}", True), ("KDA", "kda", "{:.2f}", True),
    ("Kills / 10 min", "kills_p10", "{:.2f}", True), ("Deaths / 10 min", "deaths_p10", "{:.2f}", False),
    ("Assists / 10 min", "assists_p10", "{:.2f}", True),
    ("Caught-out deaths / match", "caught_out", "{:.2f}", False),
    ("Overextended deaths / match", "overextended", "{:.2f}", False),
    ("Low-impact kill share", "low_impact_share", "{:.0%}", False),
    ("Souls / min", "souls_per_min", "{:,.0f}", True), ("Souls at 9:00", "souls_9m", "{:,.0f}", True),
    ("Souls at 15:00", "souls_15m", "{:,.0f}", True), ("Neutral souls / match", "souls_neutrals", "{:,.0f}", True),
    ("Last hits / possible", "last_hit_rate", "{:.1%}", True), ("Accuracy", "accuracy", "{:.1%}", True),
    ("Damage / min", "damage_per_min", "{:,.0f}", True),
    ("Objective damage / min", "objective_damage_per_min", "{:,.0f}", True),
    ("Avg unspent souls", "avg_unspent_souls", "{:,.0f}", False),
    ("Unsecured souls lost / match", "souls_lost_on_death", "{:,.0f}", False),
]

with tab_tiers:
    st.caption("Your matches split by the lobby's average badge, next to the leaderboard comparison players in "
               "lobbies of the same tier. Uses the hero/date filters but not the lobby-badge slider.")
    mode = st.radio("Group lobbies by", ["Rank tier", "Custom bands"], horizontal=True)
    if mode == "Rank tier":
        tier_of = lambda b: (b // 10).map(lambda t: an.RANK_NAMES[int(t)] if 0 <= t < len(an.RANK_NAMES) else None)
        order = an.RANK_NAMES
    else:
        raw = st.text_input("Band edges (lobby badge)", "0, 96, 101, 106, 111, 117",
                            help="Comma-separated; each band includes its lower edge. 101 = Ascendant 1.")
        try:
            edges = sorted({int(x) for x in raw.split(",") if x.strip()})
        except ValueError:
            st.error("Band edges must be whole numbers.")
            edges = [0, 117]
        def last_badge(hi):  # highest real badge below `hi` (subtiers run 1-6)
            tier, sub = divmod(hi - 1, 10)
            return tier * 10 + min(sub, 6) if sub else (tier - 1) * 10 + 6
        labels = [f"{an.badge_name(lo) if lo else 'Any'} – {an.badge_name(last_badge(hi))}"
                  for lo, hi in zip(edges, edges[1:])]
        tier_of = lambda b: pd.cut(b, edges, right=False, labels=labels).astype(str).replace("nan", None)
        order = labels

    you_t = pm_all[(pm_all["group"] == "You") & pm_all["match_id"].isin(match_ids_any_lobby) & pm_all["has_paths"]]
    top_t = pm_all[top_rows & pm_all["has_paths"]]
    if same_heroes:
        top_t = top_t[top_t["hero"].isin(set(hist_all.loc[hist_all["match_id"].isin(match_ids_any_lobby), "hero"]))]
    you_t = you_t.assign(tier=tier_of(you_t["average_badge"]))
    top_t = top_t.assign(tier=tier_of(top_t["average_badge"]))
    cols = [m[1] for m in TIER_METRICS]
    ys = you_t.groupby("tier")[cols].mean().assign(games=you_t.groupby("tier").size())
    ts = top_t.groupby("tier")[cols].mean().assign(games=top_t.groupby("tier").size())
    tiers = [t for t in order if t in ys.index]
    if not tiers:
        st.info("No matches with detail for the current filters.")
    else:
        min_games = st.slider("Hide tiers with fewer of your games than", 1, 30, 5)
        tiers = [t for t in tiers if ys.at[t, "games"] >= min_games] or tiers
        ys, ts = ys.reindex(tiers), ts.reindex(tiers)

        k = st.columns(len(tiers))
        for col, t in zip(k, tiers):
            col.metric(t, f"{ys.at[t, 'won']:.0%} WR", f"{int(ys.at[t, 'games'])} games", delta_color="off")

        # Gap heatmap: % difference from the leaderboard players in the same tier, signed so that green = better.
        gap = pd.DataFrame({t: [(ys.at[t, c] / ts.at[t, c] - 1) * (1 if hb else -1)
                                if pd.notna(ts.at[t, c]) and ts.at[t, c] else np.nan
                                for _, c, _, hb in TIER_METRICS] for t in tiers},
                           index=[m[0] for m in TIER_METRICS])
        text = pd.DataFrame({t: [f"{fmt.format(ys.at[t, c])}<br><sub>{TOP_SHORT}: {fmt.format(ts.at[t, c])}</sub>"
                                 if pd.notna(ts.at[t, c]) else fmt.format(ys.at[t, c])
                                 for _, c, fmt, _ in TIER_METRICS] for t in tiers}, index=gap.index)
        fig = go.Figure(go.Heatmap(
            z=gap.clip(-0.5, 0.5).to_numpy(), x=[f"{t} (you {int(ys.at[t, 'games'])} · {TOP_SHORT} "
                                                f"{int(ts.at[t, 'games']) if pd.notna(ts.at[t, 'games']) else 0})"
                                                for t in tiers],
            y=gap.index, text=text.to_numpy(), texttemplate="%{text}", colorscale="RdYlGn", zmid=0,
            zmin=-0.5, zmax=0.5, colorbar=dict(title="vs top", tickformat="+.0%"),
            hovertemplate="%{y} · %{x}<br>%{text}<br>gap %{z:+.0%}<extra></extra>"))
        fig.update_layout(title=f"You vs {TOP_SHORT} NA/EU players in the same lobby tier (green = better)",
                          height=60 + 38 * len(TIER_METRICS), yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig, width="stretch")

        label = st.selectbox("Metric across tiers", [m[0] for m in TIER_METRICS], index=6)
        c, fmt = next((m[1], m[2]) for m in TIER_METRICS if m[0] == label)
        both = pd.concat([you_t.assign(who="You"), top_t[top_t["tier"].isin(tiers)].assign(who=TOP)])
        agg = both.groupby(["tier", "who"])[c].agg(["mean", "sem", "size"]).reset_index()
        fig = px.bar(agg, x="tier", y="mean", color="who", barmode="group", error_y="sem", text="size",
                     category_orders={"tier": tiers, "who": ["You", TOP]},
                     color_discrete_map={"You": GROUP_COLORS["You"], TOP: GROUP_COLORS[TOP]},
                     title=f"{label} by lobby tier (error bars = standard error; labels = games)")
        fig.update_traces(texttemplate="%{text}", textposition="outside")
        fig.update_layout(xaxis_title=None, yaxis_title=None, legend_title=None)
        st.plotly_chart(fig, width="stretch")

        with st.expander("Table"):
            tbl = pd.concat({"You": ys, TOP: ts}, axis=1).T
            tbl.columns.name = None
            st.dataframe(tbl.rename(columns={m[1]: m[0] for m in TIER_METRICS}), width="stretch")
        st.caption("Small samples are noisy — check the game counts before reading too much into a tier.")

# --------------------------------------------------------------------------- items
with tab_items:
    it = d["items"].merge(d["me"], left_on=["match_id", "player_slot"], right_on=["match_id", "my_slot"])
    it = it[(it["type"] == "upgrade") & it["match_id"].isin(match_ids)]
    if it.empty:
        st.info("No item data for the current selection.")
    else:
        won = hist.set_index("match_id")["won"]
        first = it.sort_values("game_time_s").drop_duplicates(["match_id", "item_id"])
        first = first.assign(won=first["match_id"].map(won), minute=first["game_time_s"] / 60)
        n = first["match_id"].nunique()
        g = (first.groupby(["item", "slot", "tier"])
             .agg(games=("won", "size"), win_rate=("won", "mean"), avg_buy_min=("minute", "mean"))
             .reset_index())
        g["pick_rate"] = g["games"] / n
        min_games = st.slider("Minimum games", 1, 30, 5)
        g = g[g["games"] >= min_games].sort_values("games", ascending=False)
        st.dataframe(g, hide_index=True, width="stretch", height=520, column_config={
            "win_rate": st.column_config.ProgressColumn("Win rate", format="percent", min_value=0, max_value=1),
            "pick_rate": st.column_config.ProgressColumn("Bought in", format="percent", min_value=0, max_value=1),
            "avg_buy_min": st.column_config.NumberColumn("Avg buy (min)", format="%.1f"),
        })
        st.caption(f"Based on {n} matches with metadata. Win rate is for matches where the item was bought; "
                   "small samples are noisy.")
