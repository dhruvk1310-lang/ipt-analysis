"""Game-level strength model for padel doubles, shared by analyses 2 and 3.

Idea: every game is a small contest. Team 1 wins each game with probability
    q = sigmoid(s1 - s2),   s_team = theta_a + theta_b
    theta_i = beta * log(1 + ranking points of player i before the event) + u_i
beta turns ranking points into strength; u_i is a player's own adjustment,
learned from their games and shrunk toward 0 (ridge penalty lam) so a player
with few games stays close to what their points say.

Fitting uses games won and lost. For a "race to N" match the likelihood of a
final score is proportional to q^g1 (1-q)^g2 whatever the stopping rule, so
total games carry all the information. Match win probabilities then follow
from q and the match format (race to N, or best of 3 sets with a super
tiebreak decider).
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

import numpy as np
import pandas as pd
from scipy.optimize import brentq, minimize

from common import division_category, events, load, player_main_category, points_on

SIDES = ("team1_p1", "team1_p2", "team2_p1", "team2_p2")


# ------------------------------------------------------------------ data

def parse_segments(score: str) -> list[tuple[int, int]]:
    """"6-4, 7-6(7-2), 10-6" -> [(6, 4), (7, 6), (10, 6)] (tiebreak points dropped)."""
    if not isinstance(score, str):
        return []
    out = []
    for seg in score.split(","):
        nums = re.findall(r"\d+", seg.split("(")[0])
        if len(nums) >= 2:
            out.append((int(nums[0]), int(nums[1])))
    return out


def games_from_score(score: str, fmt: str) -> tuple[int, int] | None:
    segs = parse_segments(score)
    if not segs:
        return None
    g1 = g2 = 0
    for i, (a, b) in enumerate(segs):
        if fmt == "Best of 3 sets" and i == 2 and max(a, b) >= 10:  # super tiebreak: count as one game
            g1, g2 = g1 + (a > b), g2 + (b > a)
        else:
            g1, g2 = g1 + a, g2 + b
    return g1, g2


def match_table(slugs: list[str] | None = None, status: str = "completed") -> pd.DataFrame:
    """Matches with player ids, games, format and each player's pre-event points."""
    m = load("Matches")
    t = load("Tournaments").set_index("slug")
    if slugs is not None:
        m = m[m["tournament_slug"].isin(slugs)]
    m = m[m["status"] == status].copy()
    m["start_date"] = m["tournament_slug"].map(t["start_date"])
    if status == "completed":
        m = m[~m["result_note"].isin(["Walkover", "Retired"])]
        g = [games_from_score(s, f) for s, f in zip(m["score_team1_first"], m["format"])]
        m["g1"] = [x[0] if x else np.nan for x in g]
        m["g2"] = [x[1] if x else np.nan for x in g]
        m = m.dropna(subset=["g1", "g2", "winner"])
    return attach_points(m)


def attach_points(m: pd.DataFrame, as_of: str | None = None) -> pd.DataFrame:
    """Add f_<side> = log(1 + ranking points before the event) for each player."""
    ev = events()
    main_cat = player_main_category()
    cache = {}
    m = m.copy()
    for side in SIDES:
        vals = []
        for pid, div, date in zip(m[f"{side}_id"], m["division"], m["start_date"]):
            date = as_of or date
            if date not in cache:
                cache[date] = points_on(date, "season", ev, before=True)
            cat = division_category(div) or main_cat.get(pid, "Men's")
            pts = cache[date].get((pid, cat), 0.0) if isinstance(pid, str) else 0.0
            vals.append(float(pts))
        m[f"pts_{side}"] = vals
        m[f"f_{side}"] = np.log1p(vals)
    return m


# ------------------------------------------------------------------ model

def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


class GameModel:
    def __init__(self, use_points: bool = True, use_players: bool = True, lam: float = 2.0):
        self.use_points, self.use_players, self.lam = use_points, use_players, lam
        self.beta, self.u = 0.0, {}

    def _design(self, m: pd.DataFrame):
        ids = sorted({p for s in SIDES for p in m[f"{s}_id"] if isinstance(p, str)})
        idx = {p: i for i, p in enumerate(ids)}
        n = len(m)
        # signed player incidence: +1 for team 1, -1 for team 2
        A = np.zeros((n, len(ids)))
        F = np.zeros(n)
        for k, s in enumerate(SIDES):
            sign = 1.0 if k < 2 else -1.0
            for r, p in enumerate(m[f"{s}_id"]):
                if isinstance(p, str):
                    A[r, idx[p]] += sign
            F += sign * m[f"f_{s}"].to_numpy()
        return ids, A, F

    def fit(self, m: pd.DataFrame):
        ids, A, F = self._design(m)
        g1, g2 = m["g1"].to_numpy(float), m["g2"].to_numpy(float)
        P = len(ids)

        def unpack(w):
            beta = w[0] if self.use_points else 0.0
            u = w[1:] if self.use_players else np.zeros(P)
            return beta, u

        def loss(w):
            beta, u = unpack(w)
            x = beta * F + A @ u
            q = sigmoid(x)
            eps = 1e-12
            nll = -(g1 * np.log(q + eps) + g2 * np.log(1 - q + eps)).sum()
            nll += self.lam * (u ** 2).sum()
            r = g1 - (g1 + g2) * q  # d(-nll)/dx
            grad = np.zeros_like(w)
            if self.use_points:
                grad[0] = -(r * F).sum()
            if self.use_players:
                grad[1:] = -(A.T @ r) + 2 * self.lam * u
            return nll, grad

        w0 = np.zeros(1 + P)
        res = minimize(loss, w0, jac=True, method="L-BFGS-B")
        self.beta, u = unpack(res.x)
        self.u = dict(zip(ids, u)) if self.use_players else {}
        self.n_train = len(m)
        return self

    def game_prob(self, m: pd.DataFrame) -> np.ndarray:
        x = np.zeros(len(m))
        for k, s in enumerate(SIDES):
            sign = 1.0 if k < 2 else -1.0
            th = self.beta * m[f"f_{s}"].to_numpy() + np.array(
                [self.u.get(p, 0.0) if isinstance(p, str) else 0.0 for p in m[f"{s}_id"]])
            x += sign * th
        return sigmoid(x)

    def theta(self, pid, pts: float) -> float:
        return self.beta * math.log1p(pts) + self.u.get(pid, 0.0)

    def match_prob(self, m: pd.DataFrame) -> np.ndarray:
        q = self.game_prob(m)
        return np.array([p_match(qq, f) for qq, f in zip(q, m["format"])])


# ------------------------------------------------------------------ match formats

def p_race(q: float, n: int) -> float:
    """P(first to n games). A tiebreak at (n-1)-(n-1) is treated as one more game."""
    return sum(math.comb(n - 1 + k, k) * q ** n * (1 - q) ** k for k in range(n))


@lru_cache(maxsize=4096)
def point_prob(q: float) -> float:
    """Point-win probability that gives game-win probability q (no-ad scoring ignored)."""
    q = min(max(q, 1e-6), 1 - 1e-6)

    def game(p):
        a = p ** 4 * (1 + 4 * (1 - p) + 10 * (1 - p) ** 2)
        deuce = 20 * p ** 3 * (1 - p) ** 3 * p ** 2 / (1 - 2 * p * (1 - p))
        return a + deuce - q
    return brentq(game, 1e-6, 1 - 1e-6)


def p_first_to(p: float, n: int) -> float:
    """First to n points, win by two (a tiebreak)."""
    below = sum(math.comb(n - 1 + k, k) * p ** n * (1 - p) ** k for k in range(n - 1))
    deuce = math.comb(2 * (n - 1), n - 1) * p ** (n - 1) * (1 - p) ** (n - 1)
    return below + deuce * p ** 2 / (1 - 2 * p * (1 - p))


def p_set(q: float) -> float:
    """First to 6 games, win by 2, tiebreak at 6-6."""
    p = point_prob(q)
    tb = p_first_to(p, 7)
    below = sum(math.comb(5 + k, k) * q ** 6 * (1 - q) ** k for k in range(5))
    at55 = math.comb(10, 5) * q ** 5 * (1 - q) ** 5
    return below + at55 * (q ** 2 + 2 * q * (1 - q) * tb)


def p_match(q: float, fmt: str) -> float:
    m = re.match(r"Race to (\d+)", str(fmt))
    if m:
        return p_race(q, int(m.group(1)))
    if str(fmt).startswith("Best of 3"):
        s = p_set(q)
        stb = p_first_to(point_prob(q), 10)
        return s * s + 2 * s * (1 - s) * stb
    return p_race(q, 8)


# ------------------------------------------------------------------ scoring

def scores(p: np.ndarray, won: np.ndarray) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {"matches": int(len(p)),
            "log_loss": float(-(won * np.log(p) + (1 - won) * np.log(1 - p)).mean()),
            "brier": float(((p - won) ** 2).mean()),
            "accuracy": float(np.where(p == 0.5, 0.5, (p > 0.5) == (won == 1)).mean())}
