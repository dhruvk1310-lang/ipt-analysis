"""Analysis 2b: City Open Chandigarh 2.0 (24-27 Sep 2026) forecast.

Uses the model from a2_match_model.py (fitted on every match played so far) to:
  * give a win probability for every Chandigarh match whose teams are known
  * simulate the rest of the tournament many times, following the site's own
    rules, to get each team's chance of getting out of its group and of
    winning the title:
      - group points: 2 per win + 1 bonus for an 8-0 win (reverse-engineered
        from 250 group-standing rows at earlier events, 100% match)
      - group ties: points, then game difference, then games won, then a coin
      - knockout slots exactly as the draw lists them ("Group A - 1st" v
        "Group H - 2nd", "Winner - QF 1" ...)
    Women's Intermediate and Women's Advance share a combined round robin with
    a split bracket that the draw does not fully specify, so they get match
    probabilities only.

Predictions are frozen with a timestamp. Matches already finished when the
draw was fetched are marked and left out of the later scorecard
(a4_evaluate_chandigarh.py). Output: outputs/a3_chandigarh/.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

import numpy as np
import pandas as pd

from common import INK, LIVE, SERIES, division_category, load, out_dir, player_main_category, points_on, \
    events, save_json, setup_matplotlib, style_axes, ROOT
from padel_model import SIDES, games_from_score, p_match, sigmoid

OUT = out_dir("a3_chandigarh")
N_SIMS = 20000
SIM_DIVISIONS = ["Men's Advance", "Men's Intermediate", "Men's Beginner", "Men's 40+", "Women's Beginner"]
rng = np.random.default_rng(20260924)


def norm(s):
    return re.sub(r"\s+", " ", str(s).strip().lower())


def load_model():
    return json.loads((ROOT / "outputs/a2_match_model/final_model.json").read_text())


def team_book(matches: pd.DataFrame, entries: pd.DataFrame, start: str, model: dict) -> dict:
    """team name -> players, pre-event points and strength."""
    ev = events()
    pts = points_on(start, "season", ev, before=True)
    main_cat = player_main_category()
    beta, u = model["beta"], model["u"]
    book = {}

    def add(team, division, p1, p1n, p2, p2n):
        key = norm(team)
        if key in book or not isinstance(team, str) or re.match(r"^(group|winner|rank|tbd)", key):
            return
        cat = division_category(division)
        players = []
        for pid, pname in ((p1, p1n), (p2, p2n)):
            pid = pid if isinstance(pid, str) else None
            c = cat or main_cat.get(pid, "Men's")
            p = float(pts.get((pid, c), 0.0)) if pid else 0.0
            theta = beta * np.log1p(p) + (u.get(pid, 0.0) if pid else 0.0)
            players.append({"id": pid, "name": pname, "points": p, "theta": theta})
        book[key] = {"team": team.strip(), "division": division, "players": players,
                     "points": sum(x["points"] for x in players),
                     "strength": sum(x["theta"] for x in players)}

    for _, m in matches.iterrows():
        add(m["team1"], m["division"], m["team1_p1_id"], m["team1_p1"], m["team1_p2_id"], m["team1_p2"])
        add(m["team2"], m["division"], m["team2_p1_id"], m["team2_p1"], m["team2_p2_id"], m["team2_p2"])
    for _, e in entries.iterrows():
        add(e["team"], e["division"], e["player1_id"], e["player1"], e["player2_id"], e["player2"])
    return book


def race_outcomes(q: float, n: int = 8):
    """Distribution over final scores of a race to n: list of (g1, g2, prob)."""
    from math import comb
    out = []
    for k in range(n):
        out.append((n, k, comb(n - 1 + k, k) * q ** n * (1 - q) ** k))
        out.append((k, n, comb(n - 1 + k, k) * (1 - q) ** n * q ** k))
    tot = sum(p for *_, p in out)
    return [(a, b, p / tot) for a, b, p in out]


def simulate_division(div: str, matches: pd.DataFrame, standings: pd.DataFrame, book: dict) -> pd.DataFrame:
    dm = matches[matches["division"] == div]
    groups = {g: [norm(t) for t in s["team"]] for g, s in standings[standings["division"] == div].groupby("group")}
    teams = sorted({t for ts in groups.values() for t in ts} |
                   {norm(t) for t in pd.concat([dm["team1"], dm["team2"]]) if norm(t) in book})
    tix = {t: i for i, t in enumerate(teams)}
    T = len(teams)
    strength = np.array([book[t]["strength"] if t in book else 0.0 for t in teams])

    # ---- group stage: points, game difference, games for (N x T)
    pts = np.zeros((N_SIMS, T))
    gd = np.zeros((N_SIMS, T))
    gf = np.zeros((N_SIMS, T))
    for _, m in dm[dm["match_kind"] == "group"].iterrows():
        a, b = norm(m["team1"]), norm(m["team2"])
        if a not in tix or b not in tix:
            continue
        i, j = tix[a], tix[b]
        scored = True
        if m["status"] == "completed":
            g = games_from_score(m["score_team1_first"], m["format"])
            scored = g is not None  # a walkover with no score earns no bonus point
            g = g or (0, 0)
            w1 = np.full(N_SIMS, m["winner"] == 1)
            g1 = np.full(N_SIMS, g[0])
            g2 = np.full(N_SIMS, g[1])
        else:
            q = sigmoid(strength[i] - strength[j])
            outs = race_outcomes(q, int(re.search(r"\d+", str(m["format"]) or "8").group()) if "Race" in str(m["format"]) else 8)
            pick = rng.choice(len(outs), size=N_SIMS, p=[o[2] for o in outs])
            g1 = np.array([o[0] for o in outs])[pick]
            g2 = np.array([o[1] for o in outs])[pick]
            w1 = g1 > g2
        pts[:, i] += np.where(w1, 2 + ((g2 == 0) & scored), 0)
        pts[:, j] += np.where(~w1, 2 + ((g1 == 0) & scored), 0)
        gd[:, i] += g1 - g2
        gd[:, j] += g2 - g1
        gf[:, i] += g1
        gf[:, j] += g2

    # finishing position in group per sim
    slot = {}  # "group a - 1st" -> array of team index per sim
    pos_count = defaultdict(lambda: np.zeros(T))
    for g, members in groups.items():
        idx = np.array([tix[t] for t in members])
        key = pts[:, idx] * 1e6 + (gd[:, idx] + 1000) * 1e3 + gf[:, idx] + rng.random((N_SIMS, len(idx))) * 0.5
        order = np.argsort(-key, axis=1)
        gname = re.sub(r"^group\s*", "", norm(g))
        for place in range(len(idx)):
            who = idx[order[:, place]]
            suffix = {0: "1st", 1: "2nd", 2: "3rd"}.get(place, f"{place + 1}th")
            slot[f"group {gname} - {suffix}"] = who
            np.add.at(pos_count[place], who, 1)

    # ---- knockout
    ko = dm[dm["match_kind"] == "knockout"].copy()
    order_key = {"r16": 0, "qf": 1, "semi": 2, "sf": 2, "final": 3}
    ko["stage"] = ko["round"].map(lambda r: order_key.get(norm(r).split()[0], 9))
    reached = defaultdict(lambda: np.zeros(T))
    stage_name = {0: "reached_R16", 1: "reached_QF", 2: "reached_SF", 3: "reached_final"}
    champion = np.zeros(T)

    def resolve(label):
        n = norm(label)
        if n in slot:
            return slot[n]
        m = re.match(r"^winner - (r16|qf|sf) (\d+)$", n)
        if m:
            key = {"r16": "r16", "qf": "qf", "sf": "semi final"}[m.group(1)] + " " + m.group(2)
            return slot.get(f"winner:{key}")
        if n in tix:
            return np.full(N_SIMS, tix[n])
        return None

    for _, m in ko.sort_values("stage").iterrows():
        a, b = resolve(m["team1"]), resolve(m["team2"])
        if a is None or b is None:
            continue  # bracket slot not described in the draw
        np.add.at(reached[stage_name[m["stage"]]], a, 1)
        np.add.at(reached[stage_name[m["stage"]]], b, 1)
        if m["status"] == "completed":
            win = np.where(m["winner"] == 1, a, b)
        else:
            q = sigmoid(strength[a] - strength[b])
            p = np.vectorize(lambda x: p_match(x, m["format"]))(q)
            win = np.where(rng.random(N_SIMS) < p, a, b)
        slot[f"winner:{norm(m['round'])}"] = win
        if m["stage"] == 3:
            np.add.at(champion, win, 1)

    rows = []
    for t in teams:
        i = tix[t]
        b = book.get(t, {"team": t, "players": [{}, {}], "points": 0})
        row = {"division": div, "team": b["team"], "group": next((g for g, ms in groups.items() if t in ms), None),
               "team_points": b.get("points", 0), "strength": round(float(strength[i]), 3),
               "p_1st_in_group": pos_count[0][i] / N_SIMS, "p_top2_in_group": (pos_count[0][i] + pos_count[1][i]) / N_SIMS}
        for k in ["reached_R16", "reached_QF", "reached_SF", "reached_final"]:
            if k in reached:
                row[k] = reached[k][i] / N_SIMS
        row["p_title"] = champion[i] / N_SIMS
        rows.append(row)
    return pd.DataFrame(rows).sort_values("p_title", ascending=False)


def main():
    model = load_model()
    t = load("Tournaments").set_index("slug").loc[LIVE]
    m = load("Matches")
    m = m[m["tournament_slug"] == LIVE].copy()
    entries = load("Tournament_Entries")
    entries = entries[entries["tournament_slug"] == LIVE]
    standings = load("Group_Standings")
    standings = standings[standings["tournament_slug"] == LIVE]
    book = team_book(m, entries, t["start_date"], model)

    index = json.loads((ROOT / "raw/index.json").read_text())
    fetched_at = index[f"https://indianpadeltour.in/tournaments/{LIVE}"]["fetched_at"]
    frozen_at = pd.Timestamp.now(tz="UTC").tz_convert("Asia/Kolkata").isoformat(timespec="seconds")

    # ---- match-by-match probabilities where both teams are known
    rows = []
    for _, r in m.iterrows():
        a, b = book.get(norm(r["team1"])), book.get(norm(r["team2"]))
        if not a or not b:
            continue
        q = float(sigmoid(a["strength"] - b["strength"]))
        p = p_match(q, r["format"])
        pts_fav = 1 if a["points"] > b["points"] else 2 if b["points"] > a["points"] else 0
        rows.append({
            "match_id": r["match_id"], "division": r["division"], "round": r["round"], "date": r["date"],
            "time": r["time"], "court": r["court"], "team1": a["team"], "team2": b["team"],
            "team1_points": a["points"], "team2_points": b["points"], "format": r["format"],
            "p_team1": round(p, 4), "game_prob_team1": round(q, 4),
            "model_favourite": 1 if p > 0.5 else 2, "points_favourite": pts_fav,
            "status_at_freeze": r["status"], "winner_at_freeze": r["winner"] if r["status"] == "completed" else None,
            "in_scorecard": r["status"] != "completed",
        })
    preds = pd.DataFrame(rows)
    preds["model_disagrees_with_points"] = (preds["points_favourite"] != 0) & (preds["model_favourite"] != preds["points_favourite"])
    preds.to_csv(OUT / "match_predictions.csv", index=False)

    # ---- simulations
    sims = pd.concat([simulate_division(d, m, standings, book) for d in SIM_DIVISIONS], ignore_index=True)
    sims.to_csv(OUT / "title_odds.csv", index=False)

    # ---- freeze
    frozen = OUT / "frozen"
    frozen.mkdir(exist_ok=True)
    stamp = frozen_at[:19].replace(":", "").replace("-", "")
    preds.to_csv(frozen / f"match_predictions_{stamp}.csv", index=False)
    sims.to_csv(frozen / f"title_odds_{stamp}.csv", index=False)
    meta = {"frozen_at": frozen_at, "draw_fetched_at": fetched_at, "n_sims": N_SIMS,
            "model": {"beta": model["beta"], "lam": model["lam"], "train_matches": model["train_matches"]},
            "matches_with_prediction": len(preds), "already_played_at_freeze": int((~preds["in_scorecard"]).sum()),
            "in_scorecard": int(preds["in_scorecard"].sum()),
            "files": [f"match_predictions_{stamp}.csv", f"title_odds_{stamp}.csv"]}
    save_json(meta, frozen / "LATEST.json")

    # ---- headline facts
    fav = sims.sort_values("p_title", ascending=False).groupby("division").head(3)
    upcoming = preds[preds["in_scorecard"]]
    summary = {**meta,
               "favourites": fav[["division", "team", "team_points", "p_title"]].to_dict("records"),
               "closest_upcoming": upcoming.assign(gap=(upcoming["p_team1"] - 0.5).abs())
               .sort_values("gap").head(8)[["division", "round", "team1", "team2", "p_team1"]].to_dict("records"),
               "model_vs_points_disagreements": upcoming[upcoming["model_disagrees_with_points"]]
               [["division", "round", "team1", "team2", "team1_points", "team2_points", "p_team1"]].to_dict("records")}
    save_json(summary, OUT / "summary.json")
    charts(sims)
    print(json.dumps({k: v for k, v in meta.items() if k != "files"}, indent=1))
    print(fav.to_string())


def charts(sims: pd.DataFrame):
    plt = setup_matplotlib()
    for div in SIM_DIVISIONS:
        s = sims[sims["division"] == div].sort_values("p_title", ascending=False).head(8).iloc[::-1]
        fig, ax = plt.subplots(figsize=(7.2, 0.36 * len(s) + 1.1), dpi=200)
        style_axes(ax, "x")
        bars = ax.barh(range(len(s)), s["p_title"] * 100, color=SERIES[0], height=0.62, zorder=2)
        for b, v in zip(bars, s["p_title"]):
            ax.text(b.get_width() + 0.6, b.get_y() + b.get_height() / 2, f"{v * 100:.0f}%", va="center",
                    fontsize=8.5, color=INK["secondary"])
        ax.set_yticks(range(len(s)))
        ax.set_yticklabels(s["team"], color=INK["primary"], fontsize=8.5)
        ax.set_xlabel("Chance of winning the title (%)")
        ax.set_xlim(0, max(10, s["p_title"].max() * 100 * 1.18))
        ax.set_title(f"{div}: title chances", loc="left")
        fig.tight_layout()
        slug = re.sub(r"[^a-z0-9]+", "_", div.lower()).strip("_")
        fig.savefig(OUT / f"chart_title_{slug}.png")
        plt.close(fig)


if __name__ == "__main__":
    main()
