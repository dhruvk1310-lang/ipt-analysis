"""Analysis 1: ranking forecast (who is about to drop, and what it takes to hold on).

The site shows each player's current points and a single "defending points"
number. It does not show *when* points fall away, or what the table looks like
once they do. This script rebuilds every player's points from their dated
ranking results and projects the table forward.

Two expiry rules are modelled because the site is not consistent:
  season  : what the data shows today (window starts 1 Jul 2025 and jumps a
            year every 1 July, so everything from before 1 Jul 2026 drops on
            1 Jul 2027).
  rolling : what the site's label says ("Rolling 12 months"). Under this rule
            some results are already overdue, since results from Aug and Sep
            2025 still count today.

The projection assumes nobody earns new points, so it measures exposure, not a
prediction of the final table. Output: outputs/a1_ranking_forecast/.
"""
from __future__ import annotations

import pandas as pd

from common import (CATEGORIES, INK, SERIES, events, load, names, out_dir, points_on,
                    rank_table, save_json, setup_matplotlib, style_axes, today, window_start)

OUT = out_dir("a1_ranking_forecast")
TODAY = today()
TOP = 10


def main():
    ev = events()
    nm = names()
    rk = load("Rankings")

    # 1. The rebuild must reproduce today's rankings exactly, or nothing else holds.
    now = rank_table(points_on(TODAY, "season", ev), nm)
    chk = rk.merge(now, left_on=["player_id", "category"], right_on=["player_id", "ranking_category"],
                   how="left", suffixes=("", "_rebuilt"))
    chk["points_rebuilt"] = chk["points_rebuilt"].fillna(0)
    rebuilt_ok = int((chk["points"] == chk["points_rebuilt"]).sum())
    print(f"rebuild check: {rebuilt_ok}/{len(chk)} ranking rows reproduced")

    # 2. Points on offer: season scale x tier x division x stage, read off the
    #    results themselves. The 2026-27 season uses a lower scale than 2025-26.
    ev["scale"] = ev["awarded_on"].map(lambda d: "2026-27" if d >= pd.Timestamp("2026-07-01") else "2025-26 and earlier")
    pt = (ev.groupby(["scale", "event_type", "division", "round"])["points"]
          .agg(points=lambda s: s.mode().iloc[0], consistent=lambda s: (s == s.mode().iloc[0]).mean(), rows="size")
          .reset_index())
    stage_order = ["winner", "finalist", "semi_finalist", "quarter_finalist", "r16", "groups"]
    pt["stage_order"] = pt["round"].map({s: i for i, s in enumerate(stage_order)})
    pt = pt.sort_values(["scale", "event_type", "division", "stage_order"]).drop(columns="stage_order")
    pt.to_csv(OUT / "points_table.csv", index=False)
    scale_cmp = pt.pivot_table(index=["event_type", "division", "round"], columns="scale", values="points").reset_index()
    scale_cmp["change_pct"] = (scale_cmp["2026-27"] / scale_cmp["2025-26 and earlier"] - 1) * 100
    scale_cmp.to_csv(OUT / "points_scale_change.csv", index=False)

    # Repeat value: what each player's counting 2025-26 results would be worth
    # if they repeated them exactly under the 2026-27 scale.
    new_scale = {(r["event_type"], r["division"], r["round"]): r["points"]
                 for _, r in pt[pt["scale"] == "2026-27"].iterrows()}
    old = ev[(ev["counts_for_ranking"] == True) & (ev["scale"] != "2026-27")].copy()
    old["repriced"] = [new_scale.get((a, b, c)) for a, b, c in zip(old["event_type"], old["division"], old["round"])]
    repeat = (old.groupby(["player_id", "ranking_category"])
              .agg(old_points=("points", "sum"), repriced=("repriced", lambda s: s.sum(min_count=1)),
                   unpriced_results=("repriced", lambda s: int(s.isna().sum())))
              .reset_index())
    repeat.to_csv(OUT / "repeat_value.csv", index=False)

    # 3. Projections with no new points.
    tournaments = load("Tournaments")
    upcoming = tournaments[pd.to_datetime(tournaments["start_date"]) >= TODAY].sort_values("start_date")
    checkpoints = [("today", TODAY, "season"), ("today_if_rolling", TODAY, "rolling")]
    for _, t in upcoming.iterrows():
        checkpoints.append((f"before {t['name'].strip()}", pd.Timestamp(t["start_date"]), "rolling"))
    checkpoints += [("31 Dec 2026", pd.Timestamp("2026-12-31"), "rolling"),
                    ("30 Jun 2027", pd.Timestamp("2027-06-30"), "rolling"),
                    ("1 Jul 2027 (season reset)", pd.Timestamp("2027-07-01"), "season")]
    long = []
    for label, date, rule in checkpoints:
        tbl = rank_table(points_on(date, rule, ev), nm)
        # keep players with zero points so they still appear with a rank
        base = rk[["player_id", "category", "name"]].rename(columns={"category": "ranking_category"})
        tbl = base.merge(tbl.drop(columns=["name", "rank"]), how="left").fillna({"points": 0})
        tbl["rank"] = tbl.groupby("ranking_category")["points"].rank(method="min", ascending=False).astype(int)
        tbl["checkpoint"], tbl["date"], tbl["rule"] = label, date.date(), rule
        long.append(tbl)
    proj = pd.concat(long, ignore_index=True)
    proj.to_csv(OUT / "projections_long.csv", index=False)

    wide = proj.pivot_table(index=["ranking_category", "player_id", "name"], columns="checkpoint",
                            values=["points", "rank"], aggfunc="first")
    key_cols = ["today", "today_if_rolling", "31 Dec 2026", "30 Jun 2027", "1 Jul 2027 (season reset)"]
    top_rows = []
    for cat in CATEGORIES:
        sub = wide.loc[cat]
        sub = sub[sub[("rank", "today")] <= 20].sort_values(("rank", "today"))
        for (pid, name), r in sub.iterrows():
            row = {"category": cat, "player_id": pid, "name": name}
            for k in key_cols:
                row[f"points | {k}"] = int(r[("points", k)])
                row[f"rank | {k}"] = int(r[("rank", k)])
            top_rows.append(row)
    top = pd.DataFrame(top_rows)
    top.to_csv(OUT / "top20_projection.csv", index=False)

    # 4. Expiry schedule: every counting result and the date it stops counting.
    live = ev[(ev["counts_for_ranking"] == True)].copy()
    live["expires_rolling"] = live["awarded_on"] + pd.Timedelta(days=365)
    live["expires_season"] = pd.Timestamp("2027-07-01")
    live["already_overdue_if_rolling"] = live["expires_rolling"] <= TODAY
    live.sort_values(["ranking_category", "player_id", "awarded_on"]).to_csv(OUT / "expiry_schedule.csv", index=False)

    # 5. What it takes to hold today's rank at each checkpoint (rivals assumed to
    #    earn nothing, so this is a floor, not a guarantee).
    hold = []
    for cat in CATEGORIES:
        cur = proj[(proj["checkpoint"] == "today") & (proj["ranking_category"] == cat)]
        for cp in ["31 Dec 2026", "1 Jul 2027 (season reset)"]:
            fut = proj[(proj["checkpoint"] == cp) & (proj["ranking_category"] == cat)].set_index("player_id")
            for _, p in cur[cur["rank"] <= TOP].iterrows():
                others = fut.drop(index=p["player_id"])["points"].sort_values(ascending=False)
                target = others.iloc[p["rank"] - 1] if len(others) >= p["rank"] else 0
                mine = fut.loc[p["player_id"], "points"]
                hold.append({"category": cat, "checkpoint": cp, "name": p["name"], "rank_today": p["rank"],
                             "points_today": p["points"], "points_left": mine,
                             "points_lost": p["points"] - mine,
                             "projected_rank": int(fut.loc[p["player_id"], "rank"]),
                             "points_needed_to_hold_rank": max(0, int(target - mine))})
    hold = pd.DataFrame(hold)
    hold.to_csv(OUT / "hold_rank.csv", index=False)

    # 6. Headline facts for the report and site.
    overdue = live[live["already_overdue_if_rolling"]]
    summary = {
        "as_of": str(TODAY.date()),
        "window_start_today": str(window_start(TODAY, "season").date()),
        "rebuild_rows_ok": rebuilt_ok, "rebuild_rows": len(chk),
        "overdue_if_rolling": {"results": len(overdue), "points": int(overdue["points"].sum()),
                               "events": sorted(overdue["event"].unique().tolist())},
        "biggest_losers_by_1jul2027": hold[hold["checkpoint"] == "1 Jul 2027 (season reset)"]
        .sort_values("points_lost", ascending=False).head(8).to_dict("records"),
        "points_table_consistency": float(pt["consistent"].min()),
        "scale_change": scale_cmp.dropna().query("division == 'advance'").to_dict("records"),
    }
    for cat in CATEGORIES:
        t = top[top["category"] == cat].head(TOP)
        movers = t.assign(change=t["rank | today"] - t["rank | 31 Dec 2026"])
        summary[f"{cat} top10 rank change by 31 Dec 2026 (rolling)"] = movers[["name", "rank | today", "rank | 31 Dec 2026", "change"]].to_dict("records")
    save_json(summary, OUT / "summary.json")

    charts(top, hold)
    print(f"wrote {OUT}")


def charts(top: pd.DataFrame, hold: pd.DataFrame):
    """Dumbbell per category: points today vs left on 31 Dec 2026 and 1 Jul 2027."""
    plt = setup_matplotlib()
    for i, cat in enumerate(CATEGORIES):
        t = top[top["category"] == cat].head(12).iloc[::-1]
        fig, ax = plt.subplots(figsize=(7.2, 0.42 * len(t) + 1.3), dpi=200)
        style_axes(ax, "x")
        y = range(len(t))
        for yy, (_, r) in zip(y, t.iterrows()):
            ax.plot([r["points | 1 Jul 2027 (season reset)"], r["points | today"]], [yy, yy],
                    color=INK["axis"], linewidth=2, solid_capstyle="round", zorder=1)
        # "today" is drawn as a larger dot underneath, so it still shows as a ring
        # when a later date has the same value (no points lost)
        common_kw = dict(zorder=3, edgecolors=INK["surface"], linewidths=1.2, clip_on=False)
        ax.scatter(t["points | today"], y, s=95, color=SERIES[0], label="Today", **common_kw)
        ax.scatter(t["points | 31 Dec 2026"], y, s=40, color=SERIES[3],
                   label="31 Dec 2026, if points expire after 12 months", **common_kw)
        ax.scatter(t["points | 1 Jul 2027 (season reset)"], y, s=40, color=SERIES[1],
                   label="1 Jul 2027, season reset", **common_kw)
        ax.set_yticks(list(y))
        ax.set_yticklabels([f"#{r['rank | today']}  {r['name']}" for _, r in t.iterrows()], color=INK["primary"])
        ax.set_xlabel("Ranking points, assuming no new points are earned")
        ax.set_xlim(left=0)
        ax.set_title(f"{cat}: points still counting at each date", loc="left", pad=26)
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, fontsize=7.5, handletextpad=0.3,
                  columnspacing=1.2, borderaxespad=0.2)
        fig.tight_layout()
        fig.savefig(OUT / f"chart_dumbbell_{['mens', 'womens', 'mens40'][i]}.png")
        plt.close(fig)


if __name__ == "__main__":
    main()
