"""Analysis 2a: how predictable are IPT matches? A backtest on unseen tournaments.

Each model is trained only on tournaments that finished before the one it is
scored on:
  split A  train Mumbai City Open 5.0          -> test Goa Grand Slam 11.0
  split B  train Mumbai 5.0 + Goa GS 11.0       -> test Kochi City Open 2.0
  extra    train all three                      -> test Chandigarh 2.0 matches
           already played when the forecast was frozen (the model never saw them)

Pre-event ranking points are rebuilt from dated ranking results, because the
points printed on draw entries are current points and already include the
result being predicted.

Models compared (see padel_model.py):
  coin flip        50% every match
  points only      strength from pre-event ranking points
  results only     strength learned from games won, no ranking points
  points + results both (the model used for the Chandigarh forecast)
The penalty lam is picked on split A only; split B is the clean test.
Output: outputs/a2_match_model/.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import FINISHED, INK, LIVE, SERIES, out_dir, save_json, setup_matplotlib, style_axes
from padel_model import GameModel, match_table, scores

OUT = out_dir("a2_match_model")
LAMS = [0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256]
SPLITS = [("A: test Goa GS 11.0", FINISHED[:1], FINISHED[1]),
          ("B: test Kochi CO 2.0", FINISHED[:2], FINISHED[2])]


def models(lam):
    return {"coin flip": None,
            "points only": GameModel(use_points=True, use_players=False),
            "results only": GameModel(use_points=False, use_players=True, lam=lam),
            "points + results": GameModel(use_points=True, use_players=True, lam=lam)}


def evaluate(train, test, lam):
    rows, preds = [], {}
    won = (test["winner"] == 1).to_numpy(float)
    for name, mdl in models(lam).items():
        p = np.full(len(test), 0.5) if mdl is None else mdl.fit(train).match_prob(test)
        preds[name] = p
        rows.append({"model": name, **scores(p, won)})
    return rows, preds


def main():
    allm = match_table(FINISHED + [LIVE])
    by = {s: allm[allm["tournament_slug"] == s] for s in FINISHED + [LIVE]}
    print({s: len(v) for s, v in by.items()})

    # choose lam on split A only
    lam_rows = []
    for lam in LAMS:
        train = pd.concat([by[s] for s in SPLITS[0][1]])
        r, _ = evaluate(train, by[SPLITS[0][2]], lam)
        lam_rows += [{"lam": lam, **x} for x in r if x["model"] in ("results only", "points + results")]
    lam_df = pd.DataFrame(lam_rows)
    lam_df.to_csv(OUT / "lambda_search_split_A.csv", index=False)
    best_lam = float(lam_df[lam_df["model"] == "points + results"].sort_values("log_loss").iloc[0]["lam"])
    print("chosen lam", best_lam)

    results, pooled = [], []
    splits = SPLITS + [("Extra: Chandigarh matches played before the freeze", FINISHED, LIVE)]
    for label, tr, te in splits:
        train = pd.concat([by[s] for s in tr])
        test = by[te]
        if test.empty:
            continue
        r, preds = evaluate(train, test, best_lam)
        results += [{"split": label, "train_matches": len(train), **x} for x in r]
        if label.startswith(("A", "B")):
            d = test[["match_id", "tournament_slug", "division", "round", "team1", "team2", "winner", "format"]].copy()
            for k, v in preds.items():
                d[f"p_team1 | {k}"] = v
            pooled.append(d)
    res = pd.DataFrame(results)
    res.to_csv(OUT / "backtest_scores.csv", index=False)
    pooled = pd.concat(pooled, ignore_index=True)
    pooled.to_csv(OUT / "backtest_predictions.csv", index=False)
    print(res.to_string())

    # pooled A+B scores and calibration for the chosen model
    won = (pooled["winner"] == 1).to_numpy(float)
    pooled_scores = {k: scores(pooled[f"p_team1 | {k}"].to_numpy(), won)
                     for k in ["coin flip", "points only", "results only", "points + results"]}
    # calibrate on the favourite's side so bins are not mirror images
    p = pooled["p_team1 | points + results"].to_numpy()
    fav_p = np.where(p >= 0.5, p, 1 - p)
    fav_won = np.where(p >= 0.5, won, 1 - won)
    bins = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]
    cal = (pd.DataFrame({"p": fav_p, "won": fav_won})
           .assign(bin=lambda d: pd.cut(d["p"], bins, right=False))
           .groupby("bin", observed=True).agg(predicted=("p", "mean"), actual=("won", "mean"), matches=("p", "size"))
           .reset_index())
    cal["bin"] = cal["bin"].astype(str)
    cal.to_csv(OUT / "calibration.csv", index=False)

    # final model on everything played so far, for analysis 3
    final = GameModel(use_points=True, use_players=True, lam=best_lam).fit(allm)
    top_u = sorted(final.u.items(), key=lambda kv: kv[1], reverse=True)
    save_json({"lam": best_lam, "beta": final.beta, "train_matches": len(allm),
               "u": final.u}, OUT / "final_model.json")

    summary = {"lam": best_lam, "beta_final": final.beta, "matches_used": {s: len(v) for s, v in by.items()},
               "pooled_AB": pooled_scores, "calibration": cal.to_dict("records"),
               "biggest_positive_adjustments": top_u[:5]}
    save_json(summary, OUT / "summary.json")
    charts(res, cal)
    print(f"wrote {OUT}")


def charts(res: pd.DataFrame, cal: pd.DataFrame):
    plt = setup_matplotlib()

    # 1. log loss by model and split (lower is better)
    sub = res[res["split"].str.startswith(("A", "B"))]
    models_ = ["coin flip", "points only", "results only", "points + results"]
    splits = list(dict.fromkeys(sub["split"]))
    fig, ax = plt.subplots(figsize=(7.2, 3.3), dpi=200)
    style_axes(ax, "y")
    w = 0.36
    for i, sp in enumerate(splits):
        vals = [sub[(sub["split"] == sp) & (sub["model"] == m)]["log_loss"].iloc[0] for m in models_]
        xs = [j + (i - 0.5) * (w + 0.03) for j in range(len(models_))]
        bars = ax.bar(xs, vals, width=w, color=SERIES[i], label=sp, zorder=2)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.008, f"{v:.3f}", ha="center", va="bottom",
                    fontsize=7.5, color=INK["secondary"])
    ax.set_xticks(range(len(models_)))
    ax.set_xticklabels(models_, color=INK["primary"])
    ax.set_ylabel("Log loss (lower is better)")
    ax.set_ylim(0, 0.78)
    ax.set_title("Prediction error on tournaments the model had not seen", loc="left", pad=22)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, fontsize=8, borderaxespad=0.2)
    fig.tight_layout()
    fig.savefig(OUT / "chart_backtest_logloss.png")
    plt.close(fig)

    # 2. calibration: predicted vs actual win rate of the favourite
    fig, ax = plt.subplots(figsize=(4.6, 4.2), dpi=200)
    style_axes(ax, "both")
    ax.plot([0.5, 1], [0.5, 1], color=INK["axis"], linewidth=1.2, linestyle=(0, (4, 3)), zorder=1)
    ax.text(0.93, 0.965, "perfect", color=INK["muted"], fontsize=8, ha="right", rotation=45)
    ax.plot(cal["predicted"], cal["actual"], color=SERIES[0], linewidth=2, zorder=2)
    ax.scatter(cal["predicted"], cal["actual"], s=[max(18, n * 1.2) for n in cal["matches"]], color=SERIES[0],
               edgecolors=INK["surface"], linewidths=1.5, zorder=3)
    for _, r in cal.iterrows():
        ax.annotate(f"{int(r['matches'])}", (r["predicted"], r["actual"]), textcoords="offset points",
                    xytext=(7, -10), fontsize=7.5, color=INK["secondary"])
    ax.set_xlim(0.48, 1.0)
    ax.set_ylim(0.3, 1.02)
    ax.set_xlabel("Predicted chance the favourite wins")
    ax.set_ylabel("How often the favourite actually won")
    ax.set_title("Calibration, Goa and Kochi pooled", loc="left")
    fig.tight_layout()
    fig.savefig(OUT / "chart_calibration.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
