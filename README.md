# Deadlock Ranked Dashboard
Mainly a test for agentic work and testing out claude pro but it does work just looks ugly.

Interactive dashboard for one or more players' most recent ranked Deadlock matches, built on the
community API at <https://api.deadlock-api.com>.

## Setup

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Fetch data

```sh
.venv\Scripts\python fetch.py <steam id>            # SteamID3, SteamID64 or steamcommunity.com/profiles/... URL
.venv\Scripts\python fetch.py <steam id> --count 300
.venv\Scripts\python fetch.py <steam id> --baseline 300   # + comparison set: top-200 NA/EU players
```

Stores the latest ranked matches (default 200) plus their full metadata in `data/deadlock.db`.
Re-running only downloads matches that aren't stored yet. You can also fetch from the dashboard sidebar.
Some older matches have no metadata on the API; they still appear with summary stats.

## Run

```sh
.venv\Scripts\streamlit run app.py
```

Tabs:

- **Overview**: record, rank badge over time, rolling win rate, per-hero stats, win rate by match length.
- **Matches**: every stored match. Opening one shows the scoreboard at any minute, the team soul lead with
  objectives, mid boss and your deaths, any stat compared across all 12 players, your item build and your deaths.
- **Game state**: pick a minute to see the game state at that point across all matches: win rate by team
  soul lead, by souls vs. lane opponents and by souls rank in the lobby; wins vs. losses over time;
  comebacks and throws; a CSV export.
- **Deaths & fights**: deaths sorted into *caught out* (no living teammate within R units for the last
  15–30 s) and *overextended* (died on the enemy half with no objective taken around the death).
  Also *low-impact kill participation*, meaning no objective within 30–60 s of a kill you took part in.
  Compared against your teammates, enemies and top-200 NA/EU players. Every threshold is a slider
  under "Death & kill rules" in the sidebar.
- **Economy**: souls/min, average unspent souls (overall and through the match), unsecured souls lost on
  death, orb securing, unspent ability points and where your souls come from, against the same groups.
- **Lobby tiers**: your games split by lobby average badge (by rank tier, or your own badge bands), next
  to leaderboard players in lobbies of the same tier. The heatmap shows the gap on each metric (green =
  better); the bar chart shows one metric across tiers with error bars and sample sizes.
- **Items**: how often you buy each item, when, and the win rate when you do.

The sidebar's **Lobby average badge** range applies to every tab (both your matches and the leaderboard
comparison). **Compare against leaderboard ranks 1 – N** narrows the comparison to the top 10/25/50/100/200.

### Comparison set (top 200 NA/EU)

`--baseline N` (or the sidebar button) reads the NA and EU leaderboards and takes ranks 1–200. Each
leaderboard name lists candidate accounts, and the one with the highest current badge is kept. Entries
with more than 10 candidates (common names) are skipped, as are entries whose best candidate is below
Ascendant. It then fetches ~N ranked matches involving those players from the same period as your
matches, and only those players' own rows are compared.

### How the death and kill rules work

- Positions come from the per-second match paths; health 0 = dead.
- The enemy half is measured from each team's spawn positions; the midline is halfway between them.
- The API records only the killer of each death, not who assisted. Kill participation is therefore the
  killer plus any teammate within the participation radius of the victim when they died.

## Data

`data/deadlock.db` (SQLite):

| table | contents |
| --- | --- |
| `history` | match-history rows per tracked account (K/D/A, souls, rank badge, ranked delta) |
| `matches` | match info, plus `raw`: the full metadata JSON, zlib-compressed |
| `match_players` | all 12 players per match |
| `player_stats` | per-player stat snapshots over time (souls, damage, healing, accuracy, ...) |
| `objectives`, `mid_boss`, `deaths`, `items` | match timeline events |
| `paths` | per-second x, y, health, combat and move type per player (compressed) |
| `econ_stats` | unspent souls / AP minutes, power-up, comeback, assist souls per snapshot |
| `match_detail` | which matches have position/economy detail |
| `baseline`, `leaderboard` | comparison matches and the resolved top-200 players |
| `heroes`, `item_ref` | asset names |

The game-state views estimate stats between snapshots by straight-line interpolation. The API takes
snapshots every 3 min up to 15:00, then every 5 min.
