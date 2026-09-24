"""Analysis 2c: score the frozen Chandigarh forecast against what happened.

Run after the final (27 Sep 2026):
    python analysis/a4_evaluate_chandigarh.py            # refetches the draw first
    python analysis/a4_evaluate_chandigarh.py --no-fetch # use the cached draw

Only matches that had not been played when the forecast was frozen are scored.
Predictions are joined to results by match id, and by division + team pair if
the site has republished the draw with new ids. Baselines: a coin flip, and
"the team with more ranking points wins". Output: outputs/a4_chandigarh_scorecard/.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

import numpy as np
import pandas as pd

from common import LIVE, ROOT, load, out_dir, save_json
from padel_model import scores

OUT = out_dir("a4_chandigarh_scorecard")
FROZEN = ROOT / "outputs/a3_chandigarh/frozen"


def pair_key(div, a, b):
    return (div, " | ".join(sorted([str(a).strip().lower(), str(b).strip().lower()])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    if not a.no_fetch:
        subprocess.run([sys.executable, str(ROOT / "ipt_scraper.py"), "--refetch",
                        f"https://indianpadeltour.in/tournaments/{LIVE}"], check=True, cwd=ROOT)

    meta = json.loads((FROZEN / "LATEST.json").read_text())
    preds = pd.read_csv(FROZEN / meta["files"][0])
    odds = pd.read_csv(FROZEN / meta["files"][1])
    preds = preds[preds["in_scorecard"]]

    m = load("Matches")
    m = m[(m["tournament_slug"] == LIVE) & (m["status"] == "completed")]
    m = m[~m["result_note"].isin(["Walkover", "Retired"])]
    by_id = m.set_index("match_id")
    by_pair = {pair_key(r["division"], r["team1"], r["team2"]): r for _, r in m.iterrows()}

    rows = []
    for _, p in preds.iterrows():
        r = by_id.loc[p["match_id"]] if p["match_id"] in by_id.index else by_pair.get(pair_key(p["division"], p["team1"], p["team2"]))
        if r is None:
            continue
        team1_won = (r["team1"].strip().lower() == p["team1"].strip().lower()) == (r["winner"] == 1)
        rows.append({**p.to_dict(), "actual_score": r["score_team1_first"], "team1_won": int(team1_won)})
    res = pd.DataFrame(rows)
    if res.empty:
        print("no scored matches yet")
        save_json({"frozen_at": meta["frozen_at"], "scored_matches": 0}, OUT / "summary.json")
        return
    won = res["team1_won"].to_numpy(float)
    pts_p = np.where(res["points_favourite"] == 1, 1.0, np.where(res["points_favourite"] == 2, 0.0, 0.5))
    summary = {
        "frozen_at": meta["frozen_at"], "scored_matches": len(res), "of_predicted": len(preds),
        "model": scores(res["p_team1"].to_numpy(), won),
        "coin_flip": scores(np.full(len(res), 0.5), won),
        "higher_points_wins_accuracy": float(np.where(pts_p == 0.5, 0.5, (pts_p == 1) == (won == 1)).mean()),
        "by_division": {d: scores(g["p_team1"].to_numpy(), g["team1_won"].to_numpy(float))
                        for d, g in res.groupby("division")},
    }
    # champions: what chance did we give the eventual winner?
    finals = m[m["round"].str.lower() == "final"]
    champs = []
    for _, f in finals.iterrows():
        winner = f["team1"] if f["winner"] == 1 else f["team2"]
        o = odds[(odds["division"] == f["division"]) & (odds["team"].str.lower() == winner.strip().lower())]
        champs.append({"division": f["division"], "champion": winner,
                       "our_title_chance": float(o["p_title"].iloc[0]) if len(o) else None,
                       "our_favourite": odds[odds["division"] == f["division"]].sort_values("p_title").iloc[-1]["team"]
                       if (odds["division"] == f["division"]).any() else None})
    summary["champions"] = champs
    res.to_csv(OUT / "scored_matches.csv", index=False)
    save_json(summary, OUT / "summary.json")
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
