"""Shared helpers for the IPT analyses.

Everything reads the tables written by ipt_scraper.py into ipt_data/.
Rules of thumb used across analyses:
  * player_id is the only player key (see RUN_NOTES.md, "Player IDs").
  * Ranking points for a date are rebuilt from Event_Results, never taken from
    Tournament_Entries (entry points are *current* points, so they leak results).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "ipt_data"
OUT = ROOT / "outputs"

CATEGORIES = ["Men's", "Women's", "Men's 40+"]
FINISHED = ["mumbai-city-open-5-01", "ipt-goa-grandslam-11-0", "ipt-kochi-city-open-2-0"]
LIVE = "ipt-chandigarh-city-open-2-0"


def load(name: str) -> pd.DataFrame:
    return pd.read_csv(DATA / f"{name}.csv")


def out_dir(name: str) -> Path:
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_json(obj, path: Path):
    path.write_text(json.dumps(obj, indent=1, default=str))


# ------------------------------------------------------------------ ranking window

def window_start(date: pd.Timestamp, rule: str) -> pd.Timestamp:
    """First date whose results still count on `date`.

    season  : what the site's data shows today. The window starts 1 July of the
              year before the current season (the 2026-27 season runs from
              1 Jul 2026, so today it starts 1 Jul 2025). It jumps forward a
              year every 1 July.
    rolling : what the site's label says ("Rolling 12 months"): results count
              for 365 days after they are awarded.
    """
    date = pd.Timestamp(date)
    if rule == "season":
        season_year = date.year if date.month >= 7 else date.year - 1
        return pd.Timestamp(season_year - 1, 7, 1)
    if rule == "rolling":
        return date - pd.Timedelta(days=365)
    raise ValueError(rule)


def events() -> pd.DataFrame:
    e = load("Event_Results")
    e["awarded_on"] = pd.to_datetime(e["awarded_on"])
    return e


def points_on(date, rule: str = "season", ev: pd.DataFrame | None = None,
              before: bool = False) -> pd.Series:
    """Ranking points per (player_id, ranking_category) on `date` under `rule`.
    before=True excludes results awarded on `date` itself (pre-event points)."""
    ev = events() if ev is None else ev
    date = pd.Timestamp(date)
    lo = window_start(date, rule)
    hi = ev["awarded_on"] < date if before else ev["awarded_on"] <= date
    live = ev[(ev["awarded_on"] >= lo) & hi]
    return live.groupby(["player_id", "ranking_category"])["points"].sum()


def rank_table(points: pd.Series, names: dict) -> pd.DataFrame:
    """Competition ranking (1, 1, 3) per category, matching the site."""
    df = points.rename("points").reset_index()
    df["name"] = df["player_id"].map(names)
    df["rank"] = df.groupby("ranking_category")["points"].rank(method="min", ascending=False).astype(int)
    return df.sort_values(["ranking_category", "rank", "name"])


def division_category(division: str) -> str | None:
    """Which ranking category a draw division draws its players' points from."""
    if not isinstance(division, str):
        return None
    if "40+" in division:
        return "Men's 40+"
    if division.startswith("Women"):
        return "Women's"
    if division.startswith("Men"):
        return "Men's"
    return None  # Mixed: each player uses their own main category


def player_main_category() -> dict:
    """Best-ranked category per player (used for Mixed divisions)."""
    r = load("Rankings").sort_values(["rank"])
    order = {"Men's": 0, "Women's": 0, "Men's 40+": 1}
    r["o"] = r["category"].map(order)
    return r.sort_values(["o", "rank"]).drop_duplicates("player_id").set_index("player_id")["category"].to_dict()


def names() -> dict:
    p = load("Players")
    return dict(zip(p["player_id"], p["name"]))


# ------------------------------------------------------------------ chart style

# Reference palette (dataviz skill, light mode). Categorical order is fixed.
INK = {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
       "grid": "#e1e0d9", "axis": "#c3c2b7", "surface": "#fcfcfb"}
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DIVERGING = {"neg": "#e34948", "mid": "#f0efec", "pos": "#2a78d6"}
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def style_axes(ax, grid_axis="x"):
    """Recessive chrome: hairline grid, no top/right spines, muted ticks."""
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK["axis"])
        ax.spines[s].set_linewidth(0.8)
    ax.tick_params(colors=INK["muted"], labelcolor=INK["secondary"], labelsize=9, length=0)
    ax.set_facecolor(INK["surface"])
    if grid_axis:
        ax.grid(axis=grid_axis, color=INK["grid"], linewidth=0.8)
        ax.set_axisbelow(True)


def setup_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "figure.facecolor": INK["surface"], "savefig.facecolor": INK["surface"],
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlecolor": INK["primary"],
        "axes.labelcolor": INK["secondary"], "axes.labelsize": 9.5, "text.color": INK["primary"],
        "legend.frameon": False, "legend.fontsize": 9,
    })
    return plt


def today() -> pd.Timestamp:
    return pd.Timestamp(dt.date.today())
