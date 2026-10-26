"""SQLite storage for tracked players' ranked matches and their full match metadata."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "deadlock.db"

# Per-player time-series columns kept from match_info.players[].stats (one row per snapshot).
STAT_COLS = [
    "net_worth", "level", "kills", "deaths", "assists", "denies", "creep_kills", "neutral_kills",
    "possible_creeps", "player_damage", "player_damage_taken", "creep_damage", "neutral_damage",
    "boss_damage", "player_healing", "self_healing", "teammate_healing", "damage_mitigated",
    "damage_absorbed", "absorption_provided", "heal_prevented", "heal_lost", "max_health",
    "weapon_power", "tech_power", "ability_points", "shots_hit", "shots_missed", "hero_bullets_hit",
    "hero_bullets_hit_crit", "gold_player", "gold_player_orbs", "gold_lane_creep", "gold_lane_creep_orbs",
    "gold_neutral_creep", "gold_neutral_creep_orbs", "gold_boss", "gold_boss_orb", "gold_treasure",
    "gold_denied", "gold_death_loss", "bullet_kills", "melee_kills", "ability_kills", "headshot_kills",
]

# custom_user_stats names (looked up per match, ids are not stable) -> column
CUSTOM_STATS = {
    "Unspent Gold Minutes": "unspent_gold_minutes", "Unspent AP Minutes": "unspent_ap_minutes",
    "PowerUp Gold": "powerup_gold", "Comeback Gold": "comeback_gold",
    "Parry Success": "parry_success", "Parry Miss": "parry_miss",
}
# gold_sources (EGoldSource) -> column, for sources without a dedicated stats field
GOLD_SOURCES = {6: "gold_assists", 8: "gold_team_bonus", 12: "gold_breakable"}
ECON_COLS = [*CUSTOM_STATS.values(), *GOLD_SOURCES.values()]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS accounts (
    account_id INTEGER PRIMARY KEY,
    persona_name TEXT,
    avatar TEXT,
    last_fetched INTEGER
);
-- One row per (tracked account, ranked match) from the match-history endpoint.
CREATE TABLE IF NOT EXISTS history (
    account_id INTEGER, match_id INTEGER, hero_id INTEGER, hero_level INTEGER, start_time INTEGER,
    player_team INTEGER, match_result INTEGER, won INTEGER, player_kills INTEGER, player_deaths INTEGER,
    player_assists INTEGER, denies INTEGER, net_worth INTEGER, last_hits INTEGER, abandoned_time_s INTEGER,
    match_duration_s INTEGER, player_match_outcome INTEGER, ranked_display_badge INTEGER,
    ranked_delta INTEGER, ranked_calibration_match INTEGER, ranked_used_demotion_protection INTEGER,
    PRIMARY KEY (account_id, match_id)
);
CREATE TABLE IF NOT EXISTS matches (
    match_id INTEGER PRIMARY KEY, start_time INTEGER, duration_s INTEGER, winning_team INTEGER,
    match_outcome TEXT, average_badge_team0 INTEGER, average_badge_team1 INTEGER, average_badge INTEGER,
    banned_hero_ids TEXT,
    raw BLOB  -- zlib-compressed JSON of the full metadata record, for anything not normalised below
);
CREATE TABLE IF NOT EXISTS match_players (
    match_id INTEGER, player_slot INTEGER, account_id INTEGER, team INTEGER, hero_id INTEGER,
    kills INTEGER, deaths INTEGER, assists INTEGER, net_worth INTEGER, last_hits INTEGER, denies INTEGER,
    level INTEGER, ability_points INTEGER, assigned_lane INTEGER, abandon_match_time_s INTEGER,
    PRIMARY KEY (match_id, player_slot)
);
CREATE TABLE IF NOT EXISTS player_stats (
    match_id INTEGER, player_slot INTEGER, time_stamp_s INTEGER,
    {", ".join(f"{c} INTEGER" for c in STAT_COLS)},
    PRIMARY KEY (match_id, player_slot, time_stamp_s)
);
CREATE TABLE IF NOT EXISTS objectives (
    match_id INTEGER, team INTEGER, objective TEXT, destroyed_time_s INTEGER, first_damage_time_s INTEGER,
    player_damage INTEGER, creep_damage INTEGER
);
CREATE TABLE IF NOT EXISTS mid_boss (
    match_id INTEGER, team_killed INTEGER, team_claimed INTEGER, destroyed_time_s INTEGER
);
CREATE TABLE IF NOT EXISTS deaths (
    match_id INTEGER, player_slot INTEGER, game_time_s INTEGER, killer_player_slot INTEGER,
    death_duration_s INTEGER, time_to_kill_s REAL, x REAL, y REAL
);
CREATE TABLE IF NOT EXISTS items (
    match_id INTEGER, player_slot INTEGER, game_time_s INTEGER, item_id INTEGER, upgrade_id INTEGER,
    sold_time_s INTEGER
);
-- Per-match detail from the single-match endpoint (positions etc.). status: 'ok' or 'missing'.
CREATE TABLE IF NOT EXISTS match_detail (match_id INTEGER PRIMARY KEY, status TEXT, fetched INTEGER);
-- Per-second movement: zlib(int16 array of shape (5, n): x, y, health %, combat_type, move_type).
CREATE TABLE IF NOT EXISTS paths (match_id INTEGER, player_slot INTEGER, n INTEGER, data BLOB,
                                  PRIMARY KEY (match_id, player_slot));
-- Economy stats per snapshot that only the single-match endpoint carries (custom_user_stats, gold_sources).
CREATE TABLE IF NOT EXISTS econ_stats (
    match_id INTEGER, player_slot INTEGER, time_stamp_s INTEGER,
    {", ".join(f"{c} INTEGER" for c in ECON_COLS)},
    PRIMARY KEY (match_id, player_slot, time_stamp_s)
);
-- Matches of top leaderboard players, fetched only as a comparison baseline for a tracked account.
CREATE TABLE IF NOT EXISTS baseline (match_id INTEGER, account_id INTEGER, PRIMARY KEY (match_id, account_id));
-- Resolved top-N leaderboard players (the comparison group).
CREATE TABLE IF NOT EXISTS leaderboard (region TEXT, rank INTEGER, account_name TEXT, account_id INTEGER,
                                        badge INTEGER, fetched INTEGER, PRIMARY KEY (region, rank));
CREATE TABLE IF NOT EXISTS heroes (id INTEGER PRIMARY KEY, name TEXT, icon TEXT);
CREATE TABLE IF NOT EXISTS item_ref (
    id INTEGER PRIMARY KEY, name TEXT, type TEXT, slot TEXT, tier INTEGER, cost INTEGER, image TEXT
);
CREATE INDEX IF NOT EXISTS idx_objectives_match ON objectives(match_id);
CREATE INDEX IF NOT EXISTS idx_deaths_match ON deaths(match_id);
CREATE INDEX IF NOT EXISTS idx_items_match ON items(match_id);
CREATE INDEX IF NOT EXISTS idx_mid_boss_match ON mid_boss(match_id);
"""


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn
