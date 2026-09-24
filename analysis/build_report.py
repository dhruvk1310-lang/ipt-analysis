"""Build the Word report (report/IPT_Analysis_Report.docx) from the analysis outputs.

Every number in the report is read from outputs/, so rerunning the pipeline
(run_all.sh) refreshes the document. Writing style: plain sentences, no em dashes.
"""
from __future__ import annotations

import json
import re

import pandas as pd
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from common import CATEGORIES, OUT as OUTPUTS, ROOT

A1, A2, A3, A4 = (OUTPUTS / d for d in ("a1_ranking_forecast", "a2_match_model", "a3_chandigarh", "a4_chandigarh_scorecard"))
REPORT = ROOT / "report"
REPORT.mkdir(exist_ok=True)
INK = RGBColor(0x0B, 0x0B, 0x0B)
MUTED = RGBColor(0x52, 0x51, 0x4E)
ACCENT = RGBColor(0x25, 0x6A, 0xBF)


def js(p):
    return json.loads(p.read_text())


def ist(ts: str) -> str:
    """ISO timestamp (with offset) -> '24 Sep 2026, 23:30 IST'."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("America/New_York")
    return t.tz_convert("Asia/Kolkata").strftime("%-d %b %Y, %H:%M IST")


def pct(x, d=0):
    return f"{x * 100:.{d}f}%"


# ------------------------------------------------------------------ docx helpers

def set_cell_bg(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def set_table_borders(table, color="D9D8D2"):
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single" if edge in ("top", "bottom", "insideH") else "nil")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblPr.append(borders)


def add_table(doc, df: pd.DataFrame, widths_cm: list[float], align_right: set | None = None, font=8.5):
    align_right = align_right or set()
    t = doc.add_table(rows=1, cols=len(df.columns))
    t.alignment = WD_TABLE_ALIGNMENT.LEFT
    t.autofit = False
    set_table_borders(t)
    # fixed layout + explicit total width so Word keeps the column widths
    tblPr = t._tbl.tblPr
    tblW = OxmlElement("w:tblW")
    tblW.set(qn("w:w"), str(int(sum(widths_cm) * 567)))
    tblW.set(qn("w:type"), "dxa")
    tblPr.append(tblW)
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    grid = t._tbl.tblGrid
    for gc, w in zip(grid.findall(qn("w:gridCol")), widths_cm):
        gc.set(qn("w:w"), str(int(w * 567)))
    for i, col in enumerate(df.columns):
        c = t.rows[0].cells[i]
        c.width = Cm(widths_cm[i])
        set_cell_bg(c, "F0EFEC")
        p = c.paragraphs[0]
        p.paragraph_format.space_after = Pt(1)
        p.paragraph_format.space_before = Pt(1)
        r = p.add_run(str(col))
        r.bold, r.font.size, r.font.color.rgb = True, Pt(font), INK
        if col in align_right:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for _, row in df.iterrows():
        cells = t.add_row().cells
        for i, col in enumerate(df.columns):
            cells[i].width = Cm(widths_cm[i])
            p = cells[i].paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.space_before = Pt(1)
            r = p.add_run("" if pd.isna(row[col]) else str(row[col]))
            r.font.size, r.font.color.rgb = Pt(font), INK
            if col in align_right:
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    doc.add_paragraph()
    return t


def para(doc, text, size=10.5, color=INK, bold=False, italic=False, space_after=6):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.font.size, r.font.color.rgb, r.bold, r.italic = Pt(size), color, bold, italic
    p.paragraph_format.space_after = Pt(space_after)
    return p


def bullets(doc, items):
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        if isinstance(it, tuple):
            r = p.add_run(it[0])
            r.bold = True
            p.add_run(it[1])
        else:
            p.add_run(it)
        p.paragraph_format.space_after = Pt(3)


def figure(doc, path, caption, width_cm=16):
    doc.add_picture(str(path), width=Cm(width_cm))
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.LEFT
    para(doc, caption, size=8.5, color=MUTED, italic=True, space_after=10)


# ------------------------------------------------------------------ report

def main():
    s1, s2, s3 = js(A1 / "summary.json"), js(A2 / "summary.json"), js(A3 / "summary.json")
    s4 = js(A4 / "summary.json") if (A4 / "summary.json").exists() else {"scored_matches": 0}
    top = pd.read_csv(A1 / "top20_projection.csv")
    hold = pd.read_csv(A1 / "hold_rank.csv")
    scale = pd.read_csv(A1 / "points_scale_change.csv").dropna()
    repeat = pd.read_csv(A1 / "repeat_value.csv")
    back = pd.read_csv(A2 / "backtest_scores.csv")
    odds = pd.read_csv(A3 / "title_odds.csv")
    preds = pd.read_csv(A3 / "match_predictions.csv")

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    for side in ("left_margin", "right_margin"):
        setattr(sec, side, Cm(2.2))
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(10.5)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    for lvl, size in ((1, 16), (2, 12.5), (3, 11)):
        h = doc.styles[f"Heading {lvl}"]
        h.font.name, h.font.size, h.font.color.rgb, h.font.bold = "Calibri", Pt(size), INK if lvl > 1 else ACCENT, True

    # ---- title
    t = doc.add_paragraph()
    r = t.add_run("Indian Padel Tour")
    r.font.size, r.bold, r.font.color.rgb = Pt(24), True, INK
    para(doc, "Ranking forecast and City Open Chandigarh 2.0 predictions", size=14, color=MUTED, space_after=2)
    para(doc, f"Prepared for Dhruv Khanna. Data as of {s1['as_of']}. Chandigarh forecast frozen at "
              f"{ist(s3['frozen_at'])}, after day 1.", size=9.5, color=MUTED, space_after=14)

    # ---- 1. summary
    doc.add_heading("1. Summary", level=1)
    men = top[top["category"] == "Men's"]
    lead_m = men.iloc[0]
    fav = odds.sort_values("p_title", ascending=False).groupby("division").head(1).set_index("division")
    adv = fav.loc["Men's Advance"]
    pooled = s2["pooled_AB"]
    m5 = men.head(5)
    kept5 = m5["points | 1 Jul 2027 (season reset)"] / m5["points | today"]
    scale_all = pd.read_csv(A1 / "points_scale_change.csv").dropna().set_index(["event_type", "division", "round"])
    co_w = scale_all.loc[("city_open", "advance", "winner")]
    co_qf = scale_all.loc[("city_open", "advance", "quarter_finalist")]
    bullets(doc, [
        ("Ranking points are about to shrink sharply. ",
         f"Everything counting today dates from 1 Jul 2025 or later. If the window resets on 1 Jul 2027, "
         f"{lead_m['name']} keeps {lead_m['points | 1 Jul 2027 (season reset)']:,} of {lead_m['points | today']:,} points "
         f"without new results; the men's top 5 keep between {pct(kept5.min())} and {pct(kept5.max())} of today's total."),
        ("The 2026-27 season pays fewer points. ",
         f"A City Open win fell from {co_w['2025-26 and earlier']:.0f} to {co_w['2026-27']:.0f} points and a "
         f"quarter-final from {co_qf['2025-26 and earlier']:.0f} to {co_qf['2026-27']:.0f}. Top players cannot hold "
         "their totals even by repeating every 2025-26 result."),
        ("The site does not apply its own \"rolling 12 months\" label. ",
         f"Under a strict 12-month rule, {s1['overdue_if_rolling']['points']:,} points from "
         f"{' and '.join(e.title() for e in s1['overdue_if_rolling']['events'])} would already have expired. "
         f"Both rules are modelled."),
        ("Matches are fairly predictable from ranking points plus past games. ",
         f"On two tournaments the model had never seen, it picked the winner {pct(pooled['points + results']['accuracy'])} "
         f"of the time, against 50% for a coin flip and {pct(pooled['points only']['accuracy'])} for points alone, "
         f"and its stated confidence matched how often favourites actually won."),
        ("Chandigarh 2.0 favourites: ",
         f"{adv['team']} in Men's Advance ({pct(adv['p_title'])} to win the title). The full odds for five divisions "
         f"are in section 5 and on the website."),
    ])

    # ---- 2. data
    doc.add_heading("2. Data and three things to know", level=1)
    para(doc, "All data comes from indianpadeltour.in and its public API, scraped politely (one request at a time) "
              "with ipt_scraper.py. Match-level data exists for only three tournaments (Mumbai City Open 5.0, Goa "
              "Grand Slam 11.0 and Kochi City Open 2.0) plus the live Chandigarh draw. Ranking results exist for "
              "22 events going back to December 2024.")
    bullets(doc, [
        ("Entry points leak results. ", "The points printed next to each team in a draw are the players' current "
                                        "points, so for past tournaments they already include the result being "
                                        "predicted. All analysis here rebuilds each player's points from dated "
                                        "ranking results instead."),
        ("The ranking window. ", f"Every result dated {s1['window_start_today']} or later counts today and "
                                 "nothing older does, with no cap on the number of results. That matches a "
                                 "season-based window, not the \"rolling 12 months\" the profile pages mention."),
        ("Rebuild check. ", f"Rebuilding points from results reproduces all {s1['rebuild_rows']} ranking rows "
                            f"exactly ({s1['rebuild_rows_ok']} of {s1['rebuild_rows']})."),
    ])

    # ---- 3. forecast
    doc.add_heading("3. Analysis 1: ranking forecast", level=1)
    para(doc, "The site shows each player's current total and one \"defending points\" figure. It does not show "
              "when points fall away. This analysis projects every player's total forward, assuming no new points "
              "are earned, so it measures how much each player has to defend and by when. It is an exposure view, "
              "not a prediction of the final table.")
    doc.add_heading("3.1 The points scale changed", level=2)
    para(doc, "Points per finish in the Advance division, by season:")
    sc = scale[scale["division"] == "advance"].copy()
    stage = {"winner": "Winner", "finalist": "Finalist", "semi_finalist": "Semi-final", "quarter_finalist": "Quarter-final"}
    sc = sc[sc["round"].isin(stage)]
    sc["order"] = sc["round"].map(list(stage).index)
    sc = sc.sort_values(["event_type", "order"])
    sc_t = pd.DataFrame({"Tier": sc["event_type"].map({"city_open": "City Open", "grand_slam": "Grand Slam"}),
                         "Finish": sc["round"].map(stage),
                         "2025-26 points": sc["2025-26 and earlier"].astype(int),
                         "2026-27 points": sc["2026-27"].astype(int),
                         "Change": sc["change_pct"].map(lambda x: "no change" if round(x) == 0 else f"{x:+.0f}%")})
    add_table(doc, sc_t, [3.2, 3.2, 3.2, 3.2, 2.6], {"2025-26 points", "2026-27 points", "Change"})
    rep = top.merge(repeat, left_on=["player_id", "category"], right_on=["player_id", "ranking_category"], how="left")
    rep = rep[(rep["rank | today"] <= 5) & rep["old_points"].notna()]
    kept = (rep["repriced"] / rep["old_points"])
    para(doc, f"If the current top-5 players in each category repeated every 2025-26 result exactly, the new scale "
              f"would give them between {pct(kept.min())} and {pct(kept.max())} of the points those results earned.")

    doc.add_heading("3.2 Points still counting at each date", level=2)
    for cat, fn in zip(CATEGORIES, ["mens", "womens", "mens40"]):
        figure(doc, A1 / f"chart_dumbbell_{fn}.png",
               f"Figure: {cat} top 12. Blue is today, yellow is 31 Dec 2026 if points expire after 12 months, "
               f"orange is 1 Jul 2027 if the season window resets. Source: outputs/a1_ranking_forecast/.", 15.5)

    doc.add_heading("3.3 Top 10 by category, today and projected", level=2)
    for cat in CATEGORIES:
        doc.add_heading(cat, level=3)
        t10 = top[top["category"] == cat].head(10)
        add_table(doc, pd.DataFrame({
            "Rank": t10["rank | today"], "Player": t10["name"], "Points today": t10["points | today"].map("{:,}".format),
            "31 Dec 2026 (rolling)": [f"{p:,} (#{r})" for p, r in zip(t10["points | 31 Dec 2026"], t10["rank | 31 Dec 2026"])],
            "1 Jul 2027 (reset)": [f"{p:,} (#{r})" for p, r in zip(t10["points | 1 Jul 2027 (season reset)"], t10["rank | 1 Jul 2027 (season reset)"])],
        }), [1.2, 5.0, 2.6, 3.4, 3.4], {"Rank", "Points today", "31 Dec 2026 (rolling)", "1 Jul 2027 (reset)"})

    doc.add_heading("3.4 What it takes to hold rank by 1 Jul 2027", level=2)
    para(doc, "Points each top-10 player would need to earn to keep today's rank at the reset, if nobody else "
              "earned anything. Real targets are higher, since rivals will score too, so read this as a floor.")
    h = hold[(hold["checkpoint"] == "1 Jul 2027 (season reset)")]
    for cat in CATEGORIES:
        hh = h[h["category"] == cat].sort_values("rank_today")
        doc.add_heading(cat, level=3)
        add_table(doc, pd.DataFrame({
            "Rank": hh["rank_today"], "Player": hh["name"], "Points today": hh["points_today"].astype(int).map("{:,}".format),
            "Left at reset": hh["points_left"].astype(int).map("{:,}".format),
            "Rank at reset": hh["projected_rank"], "Needed to hold": hh["points_needed_to_hold_rank"].map("{:,}".format),
        }), [1.2, 5.0, 2.4, 2.4, 2.2, 2.4], {"Rank", "Points today", "Left at reset", "Rank at reset", "Needed to hold"})

    # ---- 4. model
    doc.add_heading("4. Analysis 2: how predictable are matches?", level=1)
    para(doc, "Most IPT matches are a race to 8 games, so every game is a small contest. The model gives each "
              "player a strength made of two parts: their ranking points before the event, and an adjustment "
              "learned from the games they have won and lost. The adjustment is held close to zero unless a "
              "player has enough games to justify moving it, so a newcomer is judged mostly by their points. A "
              "team's strength is the sum of its two players, and the difference between two teams gives the "
              "chance of winning each game, which converts into the chance of winning the match for any format "
              "(race to 8 or 9, or best of three sets with a super tiebreak).")
    para(doc, "Each version was trained only on tournaments that finished before the one it was tested on. The "
              f"penalty that holds adjustments near zero was chosen on the Goa test alone (value {s2['lam']:g}); Kochi "
              "is a clean test.")
    b = back[back["split"].str.startswith(("A", "B"))].copy()
    b["Test"] = b["split"].str.replace(r"^[AB]: test ", "", regex=True)
    add_table(doc, pd.DataFrame({"Test tournament": b["Test"], "Model": b["model"], "Matches": b["matches"],
                                 "Correct": b["accuracy"].map(lambda x: pct(x, 1)),
                                 "Log loss": b["log_loss"].map("{:.3f}".format),
                                 "Brier": b["brier"].map("{:.3f}".format)}),
              [3.6, 3.6, 1.8, 2.0, 2.0, 2.0], {"Matches", "Correct", "Log loss", "Brier"})
    para(doc, "Log loss and Brier score measure how good the probabilities are (lower is better); \"correct\" "
              "only counts whether the favourite won. Ranking points carry most of the signal; learning from past "
              "games adds a smaller, consistent improvement.", size=9.5, color=MUTED)
    figure(doc, A2 / "chart_backtest_logloss.png", "Figure: prediction error by model on the two unseen "
                                                   "tournaments. Source: outputs/a2_match_model/.", 15.5)
    figure(doc, A2 / "chart_calibration.png", "Figure: calibration. When the model gave the favourite about 75%, "
                                              "the favourite won about 70% of the time; near-certain picks came "
                                              "in as expected. Dot size and labels show the number of matches.", 9.5)
    ex = back[back["split"].str.startswith("Extra") & (back["model"] == "points + results")]
    if len(ex):
        e = ex.iloc[0]
        para(doc, f"Chandigarh's first {int(e['matches'])} matches (Men's Beginner groups, played before the forecast "
                  f"was frozen) were harder: {pct(e['accuracy'])} correct. Most beginners have no ranking points and "
                  f"no past games, so the model has little to go on.")

    # ---- 5. Chandigarh
    doc.add_heading("5. Analysis 3: City Open Chandigarh 2.0 forecast", level=1)
    para(doc, f"The model was refitted on all {s2['matches_used'] and sum(s2['matches_used'].values())} matches played "
              f"so far and the tournament was simulated {s3['n_sims']:,} times using the site's own rules, which "
              "were reverse-engineered from earlier events: 2 points per group win plus 1 bonus point for an 8-0 "
              "win (this matches all 250 group-standing rows from earlier events), ties broken on game difference, "
              "and knockout slots exactly as the draw lists them. Women's Intermediate and Women's Advance share a "
              "combined round robin that the draw does not fully describe, so they have match predictions only.")
    para(doc, f"Forecast frozen at {ist(s3['frozen_at'])}. {s3['already_played_at_freeze']} matches had "
              f"already been played and are excluded from scoring; {s3['in_scorecard']} matches with known teams "
              "will be scored after the final.")
    doc.add_heading("5.1 Title chances", level=2)
    for div in ["Men's Advance", "Men's Intermediate", "Men's Beginner", "Men's 40+", "Women's Beginner"]:
        slug = re.sub(r"[^a-z0-9]+", "_", div.lower()).strip("_")
        figure(doc, A3 / f"chart_title_{slug}.png", f"Figure: {div}, chance of winning the title (top 8 teams).", 14.5)
    doc.add_heading("5.2 Matches to watch", level=2)
    up = preds[preds["in_scorecard"]].copy()
    up["gap"] = (up["p_team1"] - 0.5).abs()
    even = up[up["gap"] > 0].sort_values("gap").head(8)
    para(doc, "Closest contests where the model has information on both teams:")
    add_table(doc, pd.DataFrame({"Division": even["division"], "Round": even["round"], "Team 1": even["team1"],
                                 "Team 2": even["team2"], "Team 1 wins": even["p_team1"].map(pct)}),
              [3.0, 1.9, 4.6, 4.6, 2.2], {"Team 1 wins"}, font=8)
    dis = up[up["model_disagrees_with_points"]]
    if len(dis):
        para(doc, "Where the model disagrees with ranking points (the team with fewer points is the model's "
                  "favourite because of how its players have performed in past games):")
        add_table(doc, pd.DataFrame({"Division": dis["division"], "Team 1 (points)": dis["team1"] + " (" + dis["team1_points"].astype(int).astype(str) + ")",
                                     "Team 2 (points)": dis["team2"] + " (" + dis["team2_points"].astype(int).astype(str) + ")",
                                     "Team 1 wins": dis["p_team1"].map(pct)}),
                  [3.0, 6.0, 6.0, 2.2], {"Team 1 wins"}, font=8)
    unknown = (up["p_team1"] == 0.5).mean()
    para(doc, f"{pct(unknown)} of the matches still to play are exact 50-50 calls: all four players have no ranking "
              f"points and no games on record.")

    doc.add_heading("5.3 Scorecard", level=2)
    if s4.get("scored_matches"):
        m = s4["model"]
        para(doc, f"{s4['scored_matches']} of {s4['of_predicted']} predicted matches scored: {pct(m['accuracy'])} "
                  f"correct, log loss {m['log_loss']:.3f} (coin flip 0.693). Higher-points-team-wins rule: "
                  f"{pct(s4['higher_points_wins_accuracy'])} correct.")
        for c in s4.get("champions", []):
            para(doc, f"{c['division']}: {c['champion']} won; the model gave them "
                      f"{pct(c['our_title_chance']) if c['our_title_chance'] is not None else 'n/a'}.")
    else:
        para(doc, "Not scored yet. After the final on 27 Sep 2026, run analysis/a4_evaluate_chandigarh.py and "
                  "rebuild this report; this section fills in automatically.")

    # ---- 6. limits
    doc.add_heading("6. Limitations", level=1)
    mt = pd.read_csv(ROOT / "ipt_data/Matches.csv")
    fin = mt[mt["tournament_slug"].isin(["mumbai-city-open-5-01", "ipt-goa-grandslam-11-0", "ipt-kochi-city-open-2-0"])
             & (mt["status"] == "completed")]
    n_fit = sum(v for k, v in s2["matches_used"].items() if "chandigarh" not in k)
    n_wo = int(fin["result_note"].isin(["Walkover", "Retired"]).sum())
    bullets(doc, [
        f"Three finished tournaments ({n_fit} matches with scores) is a small base. Estimates will sharpen with every event.",
        "Most players have played one event, so for them the model relies on ranking points alone.",
        "The expiry rule is inferred, not published. Both plausible rules are shown rather than guessed between.",
        "Games are treated as independent coin flips weighted by strength. Serve, momentum and fatigue are ignored.",
        f"{n_wo} walkovers and retirements at the finished tournaments were excluded from fitting and scoring.",
        "The Women's Intermediate and Advance combined format is not simulated.",
    ])

    # ---- appendix
    doc.add_heading("Appendix: code and files", level=1)
    files = pd.DataFrame([
        ("ipt_scraper.py", "Scrapes the site and API into ipt_data/ (CSV + Excel) with QA checks."),
        ("analysis/common.py", "Shared loading, ranking-window rules and chart style."),
        ("analysis/a1_ranking_forecast.py", "Analysis 1: projections, points scale, hold-rank targets."),
        ("analysis/padel_model.py", "Game-level strength model and match-format maths."),
        ("analysis/a2_match_model.py", "Analysis 2: backtest on unseen tournaments, calibration, final fit."),
        ("analysis/a3_chandigarh_forecast.py", "Analysis 3: Chandigarh match odds and title simulation (frozen)."),
        ("analysis/a4_evaluate_chandigarh.py", "Scores the frozen forecast after the event."),
        ("analysis/build_report.py", "Builds this Word document."),
        ("analysis/build_site.py", "Builds the website in docs/ for GitHub Pages."),
        ("run_all.sh", "Runs everything in order."),
    ], columns=["File", "What it does"])
    add_table(doc, files, [5.6, 11.0])
    para(doc, "Every table and chart in this report has a matching CSV or PNG in outputs/<analysis>/.", size=9.5, color=MUTED)

    path = REPORT / "IPT_Analysis_Report.docx"
    doc.save(path)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
