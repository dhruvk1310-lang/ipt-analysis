"""Build the website data (docs/data.json) and copy the report into docs/.

docs/ is the folder GitHub Pages serves. docs/index.html is hand-written and
reads data.json, so rerunning this script refreshes the site without touching
the page itself.
"""
from __future__ import annotations

import json
import shutil

import pandas as pd

from common import CATEGORIES, OUT as OUTPUTS, ROOT

A1, A2, A3, A4 = (OUTPUTS / d for d in ("a1_ranking_forecast", "a2_match_model", "a3_chandigarh", "a4_chandigarh_scorecard"))
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)


def js(p):
    return json.loads(p.read_text()) if p.exists() else None


def records(df: pd.DataFrame) -> list[dict]:
    return json.loads(df.to_json(orient="records"))


def main():
    s1, s2, s3, s4 = js(A1 / "summary.json"), js(A2 / "summary.json"), js(A3 / "summary.json"), js(A4 / "summary.json")
    top = pd.read_csv(A1 / "top20_projection.csv")
    hold = pd.read_csv(A1 / "hold_rank.csv")
    h = hold[hold["checkpoint"] == "1 Jul 2027 (season reset)"][["category", "name", "points_needed_to_hold_rank"]]
    top = top.merge(h, on=["category", "name"], how="left")
    forecast = {cat: records(top[top["category"] == cat].rename(columns={
        "rank | today": "rank", "points | today": "pts",
        "rank | 31 Dec 2026": "rank_dec", "points | 31 Dec 2026": "pts_dec",
        "rank | 1 Jul 2027 (season reset)": "rank_jul", "points | 1 Jul 2027 (season reset)": "pts_jul",
        "points_needed_to_hold_rank": "need_jul"})[["rank", "name", "pts", "pts_dec", "rank_dec", "pts_jul", "rank_jul", "need_jul"]])
        for cat in CATEGORIES}

    scale = pd.read_csv(A1 / "points_scale_change.csv").dropna()
    scale = scale[scale["division"] == "advance"]
    order = ["winner", "finalist", "semi_finalist", "quarter_finalist"]
    scale = scale[scale["round"].isin(order)].assign(o=lambda d: d["round"].map(order.index)).sort_values(["event_type", "o"])

    back = pd.read_csv(A2 / "backtest_scores.csv")
    cal = pd.read_csv(A2 / "calibration.csv")
    odds = pd.read_csv(A3 / "title_odds.csv")
    preds = pd.read_csv(A3 / "match_predictions.csv")
    tourn = pd.read_csv(ROOT / "ipt_data/Tournaments.csv")
    upcoming = tourn[pd.to_datetime(tourn["start_date"]) >= pd.Timestamp(s1["as_of"])].sort_values("start_date")

    data = {
        "as_of": s1["as_of"],
        "frozen_at": s3["frozen_at"],
        "ranking": {"window_start": s1["window_start_today"], "overdue_if_rolling": s1["overdue_if_rolling"],
                    "forecast": forecast,
                    "scale": records(scale[["event_type", "round", "2025-26 and earlier", "2026-27", "change_pct"]]
                                     .rename(columns={"2025-26 and earlier": "old", "2026-27": "new", "change_pct": "chg"}))},
        "model": {"lam": s2["lam"], "pooled": s2["pooled_AB"], "matches_used": s2["matches_used"],
                  "backtest": records(back), "calibration": records(cal)},
        "chandigarh": {"n_sims": s3["n_sims"], "already_played": s3["already_played_at_freeze"],
                       "in_scorecard": s3["in_scorecard"],
                       "odds": records(odds.fillna(-1)),
                       "matches": records(preds[["division", "round", "date", "time", "court", "team1", "team2",
                                                 "team1_points", "team2_points", "p_team1", "status_at_freeze",
                                                 "winner_at_freeze", "model_disagrees_with_points"]])},
        "scorecard": s4,
        "calendar": records(upcoming[["name", "type", "start_date", "city"]]),
    }
    (DOCS / "data.json").write_text(json.dumps(data, separators=(",", ":"), default=str))
    shutil.copy(ROOT / "report/IPT_Analysis_Report.docx", DOCS / "IPT_Analysis_Report.docx")
    print(f"wrote {DOCS / 'data.json'} ({(DOCS / 'data.json').stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
