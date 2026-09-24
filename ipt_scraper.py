#!/usr/bin/env python3
"""Indian Padel Tour (indianpadeltour.in) scraper.

The site is a Next.js app that server-renders its data into the React Server
Components (RSC) "flight" payload embedded in each HTML page
(self.__next_f.push(...) script tags). That payload holds the same JSON the
backend API returns, so this scraper fetches plain HTML, decodes the flight
payload and reads the JSON directly. No browser and no text parsing needed.

Stages
  crawl        fetch pages politely (one at a time, ~1.5 s apart) into raw/
  build        decode cached pages into tables, write ipt_data/*.csv + xlsx
  qa           run the checks from the brief, written into the workbook

Usage
  python ipt_scraper.py                 # full crawl + build
  python ipt_scraper.py --limit 5       # smoke test: only 5 player pages
  python ipt_scraper.py --parse-only    # rebuild tables from raw/ cache
  python ipt_scraper.py --refresh       # ignore cache, refetch everything
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque
from pathlib import Path

import pandas as pd

BASE = "https://indianpadeltour.in"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
DELAY = 1.5

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "raw"
PAGES = RAW / "pages"
INDEX = RAW / "index.json"
OUT = ROOT / "ipt_data"

CATEGORY_LABEL = {"mens": "Men's", "womens": "Women's", "mens_40": "Men's 40+"}
PROFILE_CATEGORY = {v: k for k, v in CATEGORY_LABEL.items()}

# Known display-name variants (brief section 7). Key: variant, value: rankings name.
NAME_ALIASES = {
    "paramveer bajwa": "paramveer singh bajwa",
    "swaraj deshmukh": "swaraj d",
    "saish shelkar": "saish shelar",
}

# Event-name variants in the rankings data (same date, same tier). Key: variant, value: canonical.
EVENT_ALIASES = {
    "BENGALURU CITY OPEN 2.0": "BANGALORE CITY OPEN 2.0",
}

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------- cache / fetch

class Cache:
    """Raw HTML cache. Every fetched page is kept gzipped under raw/pages/."""

    def __init__(self, refresh: bool = False):
        PAGES.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self.index = json.loads(INDEX.read_text()) if INDEX.exists() else {}
        self.last_fetch = 0.0
        self.fetched = 0
        self.refreshed: set[str] = set()

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha1(url.encode()).hexdigest()

    def path(self, url: str) -> Path:
        return PAGES / f"{self.key(url)}.html.gz"

    def has(self, url: str) -> bool:
        return url in self.index and self.path(url).exists()

    def read(self, url: str) -> str | None:
        p = self.path(url)
        return gzip.decompress(p.read_bytes()).decode("utf-8") if p.exists() else None

    def get(self, url: str) -> str | None:
        if self.has(url) and (not self.refresh or url in self.refreshed):
            return self.read(url)
        wait = DELAY - (time.time() - self.last_fetch)
        if wait > 0:
            time.sleep(wait)
        status, html = None, None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    status, html = r.status, r.read().decode("utf-8")
                break
            except urllib.error.HTTPError as e:
                status = e.code
                break
            except Exception as e:  # network hiccup: back off and retry
                print(f"  retry {attempt + 1} {url}: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        self.last_fetch = time.time()
        self.fetched += 1
        self.refreshed.add(url)
        self.index[url] = {"file": self.path(url).name, "status": status,
                           "fetched_at": dt.datetime.now().isoformat(timespec="seconds")}
        if html is not None:
            self.path(url).write_bytes(gzip.compress(html.encode("utf-8")))
        if self.fetched % 20 == 0:
            self.save()
        return html

    def save(self):
        INDEX.write_text(json.dumps(self.index, indent=1))


# ---------------------------------------------------------------- RSC decoding

PUSH_RE = re.compile(r"self\.__next_f\.push\(\[1,(\".*?\")\]\)</script>", re.S)
REF_RE = re.compile(r"^\$L?([0-9a-f]+)$")


def flight_text(html: str) -> str:
    return "".join(json.loads(c) for c in PUSH_RE.findall(html))


def parse_flight(text: str) -> dict:
    """Split an RSC flight payload into {row_id: value}."""
    b = text.encode("utf-8")
    rows, p, n = {}, 0, len(b)
    while p < n:
        c = b.find(b":", p)
        if c < 0:
            break
        rid = b[p:c].decode("utf-8", "replace").strip()
        q = c + 1
        m = re.match(rb"T([0-9a-f]+),", b[q:q + 12])
        if m:  # text row: T<hex byte length>,<raw text>
            start = q + m.end()
            length = int(m.group(1), 16)
            rows[rid] = b[start:start + length].decode("utf-8", "replace")
            p = start + length
            continue
        nl = b.find(b"\n", q)
        nl = n if nl < 0 else nl
        line = b[q:nl].decode("utf-8", "replace")
        p = nl + 1
        try:
            rows[rid] = json.loads(line)
        except ValueError:
            tag = re.match(r"^[A-Z]{1,2}(?=[\[{\"])", line)
            if tag:
                try:
                    rows[rid] = json.loads(line[tag.end():])
                    continue
                except ValueError:
                    pass
            rows[rid] = line
    return rows


class Flight:
    def __init__(self, html: str):
        self.rows = parse_flight(flight_text(html))

    def resolve(self, v, depth: int = 0):
        if depth > 60:
            return v
        if isinstance(v, str):
            m = REF_RE.match(v)
            if m and m.group(1) in self.rows and v not in ("$undefined",):
                target = self.rows[m.group(1)]
                if isinstance(target, str) and target.startswith("$S"):
                    return v
                return self.resolve(target, depth + 1)
            return v
        if isinstance(v, list):
            return [self.resolve(x, depth + 1) for x in v]
        if isinstance(v, dict):
            return {k: self.resolve(x, depth + 1) for k, x in v.items()}
        return v

    def walk(self):
        """Yield every dict and list node across all rows (unresolved)."""
        stack = list(self.rows.values())
        while stack:
            v = stack.pop()
            if isinstance(v, dict):
                yield v
                stack.extend(v.values())
            elif isinstance(v, list):
                yield v
                stack.extend(v)

    def find_key(self, key: str) -> list:
        return [self.resolve(d[key]) for d in self.walk() if isinstance(d, dict) and key in d]

    def elements(self, tag: str) -> list[tuple]:
        """React elements ["$", tag, key, props] -> [(key, resolved props)]."""
        out = []
        for node in self.walk():
            if (isinstance(node, list) and len(node) == 4 and node[0] == "$"
                    and node[1] == tag and isinstance(node[3], dict)):
                out.append((node[2], self.resolve(node[3])))
        return out

    def links(self) -> set[str]:
        return {d["href"] for d in self.walk()
                if isinstance(d, dict) and isinstance(d.get("href"), str)}


def leaves(v) -> list[str]:
    """Visible text leaves of a resolved React subtree, in document order."""
    out = []

    def rec(x):
        if isinstance(x, str):
            if x and not x.startswith("$"):
                out.append(x)
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            out.append(str(x))
        elif isinstance(x, list):
            if len(x) == 4 and x[0] == "$" and isinstance(x[3], dict):
                rec(x[3].get("children"))
            else:
                for y in x:
                    rec(y)
        elif isinstance(x, dict):
            rec(x.get("children"))

    rec(v)
    return out


# ---------------------------------------------------------------- page extractors

def extract_rankings(html: str) -> list[dict]:
    for v in Flight(html).find_key("rankings"):
        if isinstance(v, list) and v and isinstance(v[0], dict) and "items" in v[0]:
            return v
    return []


def extract_tournament_list(html: str) -> list[dict]:
    for v in Flight(html).find_key("items"):
        if isinstance(v, list) and v and isinstance(v[0], dict) and "slug" in v[0]:
            return v
    return []


def extract_tournament(html: str) -> dict | None:
    for v in Flight(html).find_key("tournament"):
        if isinstance(v, dict) and "slug" in v:
            return v
    return None


STAT_LABELS = ["Career Matches", "Wins", "Losses", "Last 30 Days", "Career Points",
               "Career Titles", "Runner-up", "Defending Points", "Best Ranking",
               "Current Streak", "Longest Streak", "Win %", "Rank", "Points"]


def extract_player(html: str) -> dict:
    f = Flight(html)
    out: dict = {"player": None, "stats": {}, "season_rows": [], "similar": [],
                 "recent_matches": [], "links": sorted(f.links())}
    for v in f.find_key("player"):
        if isinstance(v, dict) and "name" in v:
            out["player"] = v
            break
    for v in f.find_key("rows"):
        if isinstance(v, list) and v and isinstance(v[0], dict) and "tournament" in v[0]:
            out["season_rows"] = v
            break
    for v in f.find_key("players"):
        if isinstance(v, list) and v and isinstance(v[0], dict) and "display_name" in v[0]:
            out["similar"] = v
            break
    for key, props in f.elements("article"):
        if not isinstance(key, str):
            continue
        lv = leaves(props)
        if key in STAT_LABELS and lv:
            # Card layout: label, value, caption, delta  (label first)
            vals = [x for x in lv if x != key]
            out["stats"][key] = vals[0] if vals else None
        elif UUID_RE.match(key):
            hrefs = sorted({d["href"] for d in _dicts(props) if isinstance(d.get("href"), str)
                            and "match=" in d["href"]})
            out["recent_matches"].append({"match_id": key, "leaves": lv,
                                          "href": hrefs[0] if hrefs else None})
    return out


def _dicts(v):
    if isinstance(v, dict):
        yield v
        for x in v.values():
            yield from _dicts(x)
    elif isinstance(v, list):
        for x in v:
            yield from _dicts(x)


# ---------------------------------------------------------------- crawl

def player_url(pid: str) -> str:
    return f"{BASE}/players/{pid}"


def tournament_url(slug: str) -> str:
    return f"{BASE}/tournaments/{slug}"


def ranked_name_index(rankings: list[dict]) -> dict[str, str]:
    """display name (normalised) -> rankings profile id."""
    idx: dict[str, str] = {}
    for cat in rankings:
        for it in cat["items"]:
            idx.setdefault(norm_name(it["display_name"]), it.get("profile_id") or it.get("player_id"))
    return idx


def resolve_name(name: str | None, idx: dict[str, str]) -> tuple[str | None, str | None]:
    n = norm_name(name)
    if not n or n in PLACEHOLDERS:
        return None, None
    if n in idx:
        return idx[n], "exact_name"
    if NAME_ALIASES.get(n) in idx:
        return idx[NAME_ALIASES[n]], "alias"
    return None, None


API = "https://api.indianpadeltour.in/api"


def api_list(cache: Cache, path: str, fetch: bool = True) -> list[dict]:
    """All items of a paginated public API list endpoint (cached like pages)."""
    items, page = [], 1
    while page < 200:
        url = f"{API}/{path}?page={page}&limit=100"
        txt = cache.get(url) if fetch else cache.read(url)
        if not txt:
            break
        try:
            data = json.loads(txt).get("data") or {}
        except ValueError:
            break
        items += data.get("items") or []
        if not (data.get("pagination") or {}).get("has_next"):
            break
        page += 1
    return items


def member_map(api_players: list[dict]) -> dict[str, dict]:
    """Draw entry id -> API player record. The API lists, per player, every
    per-tournament registration id ("member_ids") that belongs to them."""
    return {mid: rec for rec in api_players for mid in rec.get("member_ids") or []}


def same_person(a: str | None, b: str | None) -> bool:
    """Loose check that two display names could be the same person: they share
    at least one name word (covers "Swaraj D" / "Swaraj Deshmukh", "Famaz" /
    "Famas Shanavas"; rejects "Joel Duarte" / "Mayank Daga")."""
    ta = set(re.findall(r"[a-z]+", norm_name(a)))
    tb = set(re.findall(r"[a-z]+", norm_name(b)))
    return bool(ta & tb)


def linked_record(member: dict, eid: str | None, name: str | None,
                  ranked_names: dict[str, str] | None = None) -> tuple[dict | None, bool]:
    """API record for a draw entry, unless the name printed on the registration
    matches neither the account's API name nor its rankings name (then it looks
    like someone else played on that registration). Returns (record, substituted)."""
    rec = member.get(eid) if eid else None
    if rec:
        names = {rec.get("display_name"), rec.get("name"), (ranked_names or {}).get(rec["id"])}
        if not any(same_person(name, n) for n in names if n):
            return None, True
    return rec, False


def unranked_players(tournaments: list[dict], idx: dict[str, str], api_players: list[dict]):
    """Canonical ids for draw players who are not in the rankings.

    Draw entries carry per-tournament registration ids, not profile ids, though
    /players/<entry id> still opens that person's profile. For unranked people we
    use their API player id where the API knows them, else one entry id per name
    (most recent tournament first).
    Returns (entry_id -> canonical id, name -> canonical id, every alias id)."""
    member = member_map(api_players)
    ranked = set(idx.values())
    ranked_names = {pid: n for n, pid in idx.items()}
    by_entry: dict[str, str] = {}
    by_name: dict[str, str] = {}
    alias_ids: set[str] = set(member)
    for t in sorted(tournaments, key=lambda t: t.get("startDate") or "", reverse=True):
        for d in t.get("divisions") or []:
            for e in d.get("players") or []:
                for nk, ik in (("playerOne", "playerOneId"), ("playerTwo", "playerTwoId")):
                    eid, n = e.get(ik), norm_name(e.get(nk))
                    if eid:
                        alias_ids.add(eid)
                    if not eid or not n or is_placeholder(n):
                        continue
                    rec, _ = linked_record(member, eid, n, ranked_names)
                    if (rec and rec["id"] in ranked) or resolve_name(n, idx)[0]:
                        continue
                    cid = rec["id"] if rec else by_name.get(n, eid)
                    by_entry[eid] = cid
                    by_name.setdefault(n, cid)
    return by_entry, by_name, alias_ids - set(by_entry.values())


def crawl(cache: Cache, limit: int | None):
    print("Rankings ...")
    rankings = extract_rankings(cache.get(f"{BASE}/players-standings") or "")
    for cat in rankings:
        print(f"  {cat['category']}: {len(cat['items'])} of {cat['total']}")
    idx = ranked_name_index(rankings)

    print("Tournaments ...")
    slugs = [t["slug"] for t in extract_tournament_list(cache.get(f"{BASE}/tournaments") or "")]
    cache.get(f"{BASE}/")  # home page, kept in the raw cache for reference
    # the public API lists more tournaments than the /tournaments page shows
    slugs += [t["slug"] for t in api_list(cache, "tournaments")]
    api_players = api_list(cache, "players")
    print(f"  API: {len(api_players)} player records")
    seen_t: set[str] = set()
    tournaments: list[dict] = []
    t_q = deque()

    def queue_t(slug):
        if slug not in seen_t:
            seen_t.add(slug)
            t_q.append(slug)

    def drain_tournaments():
        while t_q:
            slug = t_q.popleft()
            h = cache.get(tournament_url(slug))
            t = extract_tournament(h or "") if h else None
            print(f"  tournament {slug}: {'ok' if t else 'NO DATA'}")
            if t:
                tournaments.append(t)

    for slug in slugs:
        queue_t(slug)
    drain_tournaments()

    player_q: deque[str] = deque()
    seen_p: set[str] = set()

    def queue_p(pid):
        if pid and pid not in seen_p:
            seen_p.add(pid)
            player_q.append(pid)

    for cat in rankings:
        for it in cat["items"]:
            queue_p(it.get("profile_id") or it.get("player_id"))
    by_entry, _, entry_ids = unranked_players(tournaments, idx, api_players)
    member = member_map(api_players)
    for pid in by_entry.values():
        if pid not in member:  # an entry id that belongs to another account opens that account's page
            queue_p(pid)

    print(f"Players ... ({len(player_q)} queued: ranked + {len(set(by_entry.values()))} unranked draw players)")
    done = 0
    while player_q:
        if limit is not None and done >= limit:
            print(f"  --limit {limit} reached, {len(player_q)} player pages left unvisited")
            break
        pid = player_q.popleft()
        h = cache.get(player_url(pid))
        done += 1
        if done % 50 == 0:
            print(f"  {done} player pages ({len(player_q)} queued, {cache.fetched} fetched this run)", flush=True)
        if not h:
            continue
        p = extract_player(h)
        for sp in p["similar"]:
            queue_p(sp.get("profile_id") or sp.get("player_id"))
        for href in p["links"]:
            m = re.match(r"^/tournaments/([^/?#]+)", href)
            if m:
                queue_t(m.group(1))
            m = re.match(r"^/players/([^/?#]+)", href)
            if m and m.group(1) not in entry_ids:  # entry ids are aliases of known people
                queue_p(m.group(1))
        if t_q:
            drain_tournaments()
            by_entry, _, entry_ids = unranked_players(tournaments, idx, api_players)
            for pid2 in by_entry.values():
                if pid2 not in member:
                    queue_p(pid2)
    cache.save()
    print(f"Crawl done: {cache.fetched} pages fetched this run, {len(cache.index)} in cache")


# ---------------------------------------------------------------- build tables

def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip().replace(",", "").replace("#", "").replace("%", "").replace("₹", "")
    if s in ("", "-", "–"):
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return None


def norm_name(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


PLACEHOLDERS = {"tbd", "tba", "bye", ""}
# knockout slots before they are filled: "Group A - 1st", "Winner - QF 1", "Rank 2"
SLOT_RE = re.compile(r"^(group [a-z0-9]+ - \d+(st|nd|rd|th)|(winner|loser) - .+|rank \d+|best \d+(st|nd|rd|th).*)$")


def is_placeholder(name: str | None) -> bool:
    n = norm_name(name)
    return n in PLACEHOLDERS or bool(SLOT_RE.match(n))


def split_team(team: str | None) -> list[str]:
    return [p.strip() for p in (team or "").split("/")] if team else []


def real_name(x: str | None) -> str | None:
    """Drop draw placeholders (TBD, "Group A - 1st", "Winner - QF 1") so they are not treated as players."""
    return None if x is None or is_placeholder(x) else x


def division_parts(name: str | None) -> tuple[str | None, str | None]:
    """"Men's Advance" -> ("Men's", "advance"); "Men's 40+" -> ("Men's 40+", "open")."""
    if not name:
        return None, None
    n = name.strip()
    if "40+" in n:
        return "Men's 40+", "open"
    m = re.match(r"^(Men's|Women's|Mixed)\s*(.*)$", n, re.I)
    if not m:
        return None, n.lower()
    return m.group(1), (m.group(2).strip().lower() or "open")


def event_key(s: str | None) -> str:
    """Normalise event titles so rankings names and tournament titles meet."""
    s = norm_name(s)
    s = s.replace("grandslam", "grand slam")
    s = re.sub(r"[^a-z0-9. ]", " ", s)
    words = sorted(w for w in s.split() if w not in ("ipt", "edition", "city", "the"))
    return " ".join(words)


def build(cache: Cache):
    OUT.mkdir(exist_ok=True)
    issues: list[dict] = []

    # ---- rankings
    rankings = extract_rankings(cache.read(f"{BASE}/players-standings") or "")
    rank_rows, event_rows = [], []
    for cat in rankings:
        label = CATEGORY_LABEL.get(cat["category"], cat["category"])
        for it in cat["items"]:
            pid = it.get("profile_id") or it.get("player_id")
            rank_rows.append({
                "category": label, "category_code": cat["category"], "rank": it["rank"],
                "rank_overall": it.get("rank_overall"), "player_id": pid,
                "user_uuid": it.get("player_id"), "name": it["display_name"],
                "points": it.get("points_12m"), "points_overall": it.get("points_overall"),
                "move": it.get("movement"), "events": it.get("tournaments_played"),
                "gender": it.get("gender"), "window_start": cat.get("window_start"),
                "url": player_url(pid),
            })
            for r in it.get("results") or []:
                event_rows.append({
                    "player_id": pid, "player": it["display_name"], "ranking_category": label,
                    "event": r.get("event_name"), "event_type": r.get("event_tier"),
                    "awarded_on": r.get("awarded_on"), "month": (r.get("awarded_on") or "")[:7] or None,
                    "division": r.get("division_level"), "round": r.get("stage"),
                    "points": r.get("points"), "counts_for_ranking": r.get("counts_for_ranking"),
                })
    Rankings = pd.DataFrame(rank_rows)
    idx = ranked_name_index(rankings)

    # ---- tournaments, divisions, entries, matches, standings
    t_urls = sorted(u for u in cache.index if re.match(rf"^{re.escape(BASE)}/tournaments/[^/?#]+$", u))
    listing = {t["slug"]: t for t in extract_tournament_list(cache.read(f"{BASE}/tournaments") or "")}
    tournaments = []
    for u in t_urls:
        html = cache.read(u)
        t = extract_tournament(html or "") if html else None
        if t:
            tournaments.append(t)
        else:
            issues.append({"type": "tournament_page_no_data", "detail": u})
    api_players = api_list(cache, "players", fetch=False)
    api_tournaments = api_list(cache, "tournaments", fetch=False)
    api_name = {r["id"]: r.get("display_name") for r in api_players}
    ranked_name_by_id = {r["player_id"]: r["name"] for r in rank_rows}
    ranked_ids = set(idx.values())
    # API display name -> canonical id, only where the name is unambiguous
    _api_names = defaultdict(set)
    for rec in api_players:
        cid = rec["id"] if rec["id"] in ranked_ids else (resolve_name(rec.get("display_name"), idx)[0] or rec["id"])
        _api_names[norm_name(rec.get("display_name"))].add(cid)
    api_name_idx = {n: next(iter(v)) for n, v in _api_names.items() if n and len(v) == 1}
    member = member_map(api_players)
    by_entry, by_name, entry_ids = unranked_players(tournaments, idx, api_players)
    unmatched = Counter()
    conflicts = []
    substitutions: set = set()

    def pid_for(name, eid=None):
        """Draw/match player -> (player_id, how). Ranked players get their rankings
        profile id, unranked players a canonical API or entry id. The API's
        member_ids link is used first; display-name matching is the fallback."""
        n = norm_name(name)
        if not n or is_placeholder(n):
            return None, None
        rec, substituted = linked_record(member, eid, name, ranked_name_by_id)
        if substituted:
            substitutions.add((name, eid, member[eid]["id"]))
        name_pid, how = resolve_name(name, idx)
        if rec and rec["id"] in ranked_ids:
            if name_pid and name_pid != rec["id"]:
                # the printed name is a different ranked player: trust the name
                conflicts.append((name, eid, rec["id"], name_pid))
                return name_pid, "name_over_api_conflict"
            return rec["id"], "api_member_ids"
        if name_pid:
            return name_pid, (f"{how}_guest_record" if rec and rec.get("is_guest") else how)
        if eid in by_entry:
            return by_entry[eid], ("unranked_api_id" if rec else
                                   "substitute_entry_id" if substituted else "unranked_entry_id")
        if n in by_name:
            return by_name[n], "unranked_name"
        if n in api_name_idx:  # e.g. a late draw change with no entry record
            return api_name_idx[n], "api_display_name"
        unmatched[name] += 1
        return None, "unmatched"

    def lookup(name):
        """Side-effect free name -> id (for checks, not for linking)."""
        n = norm_name(name)
        return resolve_name(name, idx)[0] or by_name.get(n) or api_name_idx.get(n)

    t_rows, div_rows, entry_rows, match_rows, stand_rows = [], [], [], [], []
    for t in tournaments:
        slug = t["slug"]
        t_rows.append({
            "slug": slug, "name": t.get("title"), "type": t.get("category"),
            "status": t.get("status"), "start_date": t.get("startDate"), "end_date": t.get("endDate"),
            "dates": t.get("date"), "city": t.get("city"), "venue": t.get("venue"),
            "prize_inr": num(t.get("prize")), "format": t.get("format"), "courts": t.get("courts"),
            "balls": t.get("balls"), "turf": t.get("turf"), "source_event_id": t.get("sourceEventId"),
            "divisions": len(t.get("divisions") or []), "in_listing": slug in listing,
            "in_api": slug in {x["slug"] for x in api_tournaments},
            "url": tournament_url(slug),
        })
        # team -> player ids across the whole tournament: linked divisions share
        # one draw, so a match can reference entries listed under a sibling division
        team_ids: dict[str, tuple] = {}
        for d in t.get("divisions") or []:
            for e in d.get("players") or []:
                team_ids.setdefault(norm_name(e.get("team")), (e.get("playerOneId"), e.get("playerTwoId"),
                                                               e.get("playerOne"), e.get("playerTwo")))
        div_by_id = {d.get("id"): d for d in t.get("divisions") or []}
        for d in t.get("divisions") or []:
            gender, level = division_parts(d.get("name"))
            div_rows.append({
                "division_id": d.get("id"), "tournament_slug": slug, "event": t.get("title"),
                "division": d.get("name"), "gender": gender, "level": level,
                "team_count": d.get("teamCount"), "status": d.get("status"), "format": d.get("format"),
                "match_format": d.get("matchFormat"), "qualify_per_group": d.get("qualifyPerGroup"),
                "matches_listed": len(d.get("schedule") or []),
            })
            for e in d.get("players") or []:
                entry_rows.append({
                    "tournament_slug": slug, "event": t.get("title"), "division": d.get("name"),
                    "gender": gender, "level": level, "seed": e.get("seed"), "team": e.get("team"),
                    "player1_id": pid_for(e.get("playerOne"), e.get("playerOneId"))[0], "player1": e.get("playerOne"),
                    "player2_id": pid_for(e.get("playerTwo"), e.get("playerTwoId"))[0], "player2": e.get("playerTwo"),
                    "player1_entry_id": e.get("playerOneId"), "player2_entry_id": e.get("playerTwoId"),
                    "player1_points": e.get("playerOnePoints"), "player2_points": e.get("playerTwoPoints"),
                    "team_points": e.get("points"), "entry_rank": e.get("rank"), "entry_status": e.get("status"),
                })
            for grp, lst in (d.get("standings") or {}).items():
                for pos, s in enumerate(lst or [], 1):
                    stand_rows.append({
                        "tournament_slug": slug, "division": d.get("name"), "group": grp, "position": pos,
                        "team": s.get("team"), "played": s.get("played"), "wins": s.get("wins"),
                        "losses": s.get("losses"), "games_for": s.get("gamesFor"),
                        "games_against": s.get("gamesAgainst"), "difference": num(s.get("difference")),
                        "bonus_points": s.get("bonusPoints"), "points": s.get("points"),
                        "qualification": s.get("qualification"),
                    })
            for m in d.get("schedule") or []:
                own = div_by_id.get(m.get("divisionId"), d)
                row = match_row(m, t, own, team_ids)
                row["listed_under"] = d.get("name")
                match_rows.append(row)
    Tournaments = pd.DataFrame(t_rows)
    Divisions = pd.DataFrame(div_rows)
    Entries = pd.DataFrame(entry_rows)
    Standings = pd.DataFrame(stand_rows)

    # ---- players
    # A player page counts if it is a rankings id, the canonical entry id of an
    # unranked draw player, or any other discovered id. Other draw entry ids are
    # aliases of people already covered, so their pages are skipped.
    unranked_ids = set(by_entry.values())
    api_by_id = {rec["id"]: rec for rec in api_players}
    p_urls = sorted(u for u in cache.index if u.startswith(f"{BASE}/players/")
                    and (u.rsplit("/", 1)[1] in ranked_ids | unranked_ids
                         or u.rsplit("/", 1)[1] not in entry_ids))
    player_rows, card_rows, season_rows, card_matches = [], [], [], []
    rank_by_pid = defaultdict(list)
    for r in rank_rows:
        rank_by_pid[r["player_id"]].append(r)
    for u in p_urls:
        pid = u.rsplit("/", 1)[1]
        html = cache.read(u)
        if not html:
            issues.append({"type": "player_page_missing", "detail": u,
                           "status": cache.index.get(u, {}).get("status")})
            continue
        p = extract_player(html)
        pl, st = p["player"] or {}, p["stats"]
        if not pl:
            issues.append({"type": "player_page_no_data", "detail": u})
        ranks = rank_by_pid.get(pid, [])
        prof_code = PROFILE_CATEGORY.get(pl.get("category"))
        primary = next((r for r in ranks if r["category_code"] == prof_code), None) \
            or (min(ranks, key=lambda r: r["rank"]) if ranks else None)
        last30 = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(pl.get("last30Days") or st.get("Last 30 Days") or ""))
        prof_rank, prof_points = num(pl.get("rank")), num(pl.get("points"))
        mismatch = bool(primary) and (prof_code != primary["category_code"]
                                      or prof_rank != primary["rank"] or prof_points != primary["points"])
        player_rows.append({
            "player_id": pid, "user_uuid": primary["user_uuid"] if primary else None,
            "name": primary["name"] if primary else pl.get("name"),
            "category": primary["category"] if primary else None,
            "rank": primary["rank"] if primary else None,
            "rank_move": primary["move"] if primary else None,
            "points": primary["points"] if primary else None,
            "win_pct": num(pl.get("winPercentage")), "career_matches": num(pl.get("careerMatches")),
            "wins": num(pl.get("wins")), "losses": num(pl.get("losses")),
            "last30_wins": int(last30.group(1)) if last30 else None,
            "last30_matches": int(last30.group(2)) if last30 else None,
            "titles": num(pl.get("careerTitles")), "runner_up": num(st.get("Runner-up")),
            "defending_points": num(st.get("Defending Points")), "best_rank": num(st.get("Best Ranking")),
            "current_streak": num(st.get("Current Streak")), "longest_streak": num(st.get("Longest Streak")),
            "career_points": num(pl.get("careerPoints")),
            "events": primary["events"] if primary else None,
            "url": u,
            "ranked_categories": ", ".join(r["category"] for r in ranks) or None,
            "profile_name": pl.get("name"), "profile_category": pl.get("category"),
            "profile_rank": prof_rank, "profile_points": prof_points,
            "profile_mismatch": mismatch,
            "api_is_guest": api_by_id.get(pid, {}).get("is_guest"),
            "api_best_rank": api_by_id.get(pid, {}).get("best_rank"),
            "api_events": ", ".join(api_by_id.get(pid, {}).get("events") or []) or None,
            "id_form": ("api_player" if pid in api_by_id and pid not in ranked_ids
                        else "draw_entry" if pid in unranked_ids
                        else "uuid" if UUID_RE.match(pid) else "slug"),
        })
        if mismatch:
            issues.append({"type": "profile_differs_from_rankings", "player_id": pid,
                           "detail": f"{pl.get('name')}: profile says {pl.get('category')} #{prof_rank} "
                                     f"{prof_points} pts; rankings say {primary['category']} #{primary['rank']} "
                                     f"{primary['points']} pts"})
        for sp in p["similar"]:
            card_rows.append({"found_on": pid, "player_id": sp.get("profile_id") or sp.get("player_id"),
                              "name": sp.get("display_name"), "rank": sp.get("rank"),
                              "points": sp.get("points_12m"), "events": sp.get("tournaments_played")})
        for r in p["season_rows"]:
            season_rows.append({"player_id": pid, "event": r.get("tournament"), "division": r.get("category"),
                                "partner": r.get("partner"), "round": r.get("round"), "status": r.get("status"),
                                "points": r.get("points"), "wins": r.get("wins"), "losses": r.get("losses"),
                                "prize": r.get("prize")})
        for rm in p["recent_matches"]:
            card_matches.append({"player_id": pid, **rm})
    # unranked people: prefer the name printed in draws (profiles sometimes hold a
    # short handle like "arshdeep"); people with no usable profile page get a row
    # with the stats left empty
    draw_names = defaultdict(Counter)
    for e in entry_rows:
        for k in ("1", "2"):
            if e[f"player{k}_id"] and e[f"player{k}"]:
                draw_names[e[f"player{k}_id"]][e[f"player{k}"].strip()] += 1
    have_rows = {r["player_id"] for r in player_rows}
    for r in player_rows:
        if r["player_id"] not in ranked_ids and draw_names.get(r["player_id"]):
            r["name"] = draw_names[r["player_id"]].most_common(1)[0][0]
    for pid_ in sorted(set(by_entry.values()) - have_rows):
        if draw_names.get(pid_):
            player_rows.append({"player_id": pid_, "name": draw_names[pid_].most_common(1)[0][0],
                                "id_form": "draw_only", "url": None})
    Players = pd.DataFrame(player_rows)
    Cards = pd.DataFrame(card_rows).drop_duplicates("player_id") if card_rows else pd.DataFrame()
    Season = pd.DataFrame(season_rows)

    # ---- matches: tournament schedules first, player-page cards as fallback
    Matches = pd.DataFrame(match_rows)
    if not Matches.empty:
        listed = Matches.groupby("match_id")["listed_under"].agg(lambda x: ", ".join(sorted(set(x))))
        before = len(Matches)
        Matches = Matches.drop_duplicates("match_id").copy()
        Matches["listed_under"] = Matches["match_id"].map(listed)
        if before != len(Matches):
            issues.append({"type": "match_listed_in_linked_divisions",
                           "detail": f"{before - len(Matches)} duplicate listings dropped (same match_id "
                                     f"under linked divisions); see Matches.listed_under"})
    known = set(Matches["match_id"]) if not Matches.empty else set()
    extra, stale = {}, set()
    scraped = {r["slug"] for r in t_rows}
    for cm in card_matches:
        if cm["match_id"] not in known and cm["match_id"] not in extra:
            row = card_match_row(cm)
            if not row:
                continue
            if row["tournament_slug"] in scraped:
                # the draw page is the source of truth; a card id it does not list
                # is stale (the draw was republished after the profile was cached)
                stale.add((row["tournament_slug"], cm["match_id"]))
                continue
            extra[cm["match_id"]] = row
    for slug in sorted({s for s, _ in stale}):
        n = sum(1 for s, _ in stale if s == slug)
        issues.append({"type": "stale_player_page_match_ids",
                       "detail": f"{n} recent-match cards point to {slug} match ids that its draw no longer "
                                 f"lists (draw republished after those profiles were fetched); ignored"})
    if extra:
        Matches = pd.concat([Matches, pd.DataFrame(extra.values())], ignore_index=True)
    card_found = defaultdict(set)
    for cm in card_matches:
        card_found[cm["match_id"]].add(cm["player_id"])
    if not Matches.empty:
        Matches["on_player_pages"] = Matches["match_id"].map(lambda x: len(card_found.get(x, ())))

    # ---- name -> player_id resolution for match players
    if not Matches.empty:
        for side in ("team1_p1", "team1_p2", "team2_p1", "team2_p2"):
            res = [pid_for(n, e if isinstance(e, str) else None) if isinstance(n, str) else (None, None)
                   for n, e in zip(Matches[side], Matches.get(f"{side}_entry_id", [None] * len(Matches)))]
            Matches[f"{side}_id"] = [r[0] for r in res]
            Matches[f"{side}_id_source"] = [r[1] for r in res]
        cols = list(Matches.columns)
        lead = [c for c in cols if not c.endswith("_entry_id")]
        Matches = Matches[lead + [c for c in cols if c.endswith("_entry_id")]]
    Unmatched = pd.DataFrame([{"name": k, "appearances": v} for k, v in unmatched.most_common()],
                             columns=["name", "appearances"])

    for name, eid, api_pid, name_pid in sorted(set(conflicts)):
        issues.append({"type": "id_link_conflict", "player_id": api_pid,
                       "detail": f"{name}: entry {eid} belongs to {api_pid} per API member_ids, "
                                 f"but the printed name is ranked player {name_pid}; name used"})
    for name, eid, acct in sorted(substitutions):
        issues.append({"type": "entry_name_differs_from_account", "player_id": acct,
                       "detail": f"draw prints '{name}' on registration {eid}, which the API links to "
                                 f"account {acct} ({api_name.get(acct)}); names share nothing, so it was "
                                 f"not linked to that account"})
    for rec in api_players:
        rn = ranked_name_by_id.get(rec["id"])
        if rn and not same_person(rn, rec.get("display_name")):
            issues.append({"type": "account_name_differs", "player_id": rec["id"],
                           "detail": f"one id, two names: rankings say '{rn}', the API and newer draws say "
                                     f"'{rec.get('display_name')}'. Kept as one player_id (the site links them); "
                                     f"whether this is a rename or a reused account is not knowable from the site"})

    # ---- player-page match cards vs tournament schedule (score perspective)
    by_id = Matches.set_index("match_id") if not Matches.empty else pd.DataFrame()
    cc_rows = []
    for cm in card_matches:
        if cm["match_id"] not in known:
            continue
        m = by_id.loc[cm["match_id"]]
        c = card_match_row(cm) or {}
        owner = cm["player_id"]
        side = next((k for k in (1, 2) if owner in (m[f"team{k}_p1_id"], m[f"team{k}_p2_id"])), None)
        label = next((x for x in cm["leaves"][2:4] if x in ("Win", "Loss")), None)
        cc_rows.append({
            "match_id": cm["match_id"], "player_id": owner, "card_team1": c.get("team1"),
            "schedule_team1": m["team1"], "card_score": c.get("score_team1_first"),
            "schedule_score": m["score_team1_first"], "owner_side_in_schedule": side,
            "card_label": label, "schedule_winner": m["winner"],
            "same_order": norm_name(c.get("team1")) == norm_name(m["team1"]),
            "owner_first_on_card": owner in (lookup(n) for n in split_team(c.get("team1"))),
            "label_agrees": (label is None or side is None or pd.isna(m["winner"])
                             or (label == "Win") == (m["winner"] == side)),
        })
    CardChecks = pd.DataFrame(cc_rows)

    # ---- Event_Results: rankings results, plus partner / W-L from draws and matches
    Events = pd.DataFrame(event_rows)
    Events = Events.drop_duplicates(["player_id", "event", "awarded_on", "division", "round", "points"])
    ek_to_slug = {event_key(r["name"]): r["slug"] for r in t_rows}
    Events["tournament_slug"] = Events["event"].map(lambda e: ek_to_slug.get(event_key(e)))
    Events["event_canonical"] = Events["event"].map(lambda e: EVENT_ALIASES.get(e, e))
    for variant, canonical in EVENT_ALIASES.items():
        n = int((Events["event"] == variant).sum())
        if n:
            issues.append({"type": "event_name_variant",
                           "detail": f"rankings name the same event two ways: '{variant}' ({n} rows) and "
                                     f"'{canonical}'; see Event_Results.event_canonical"})
    partner_draw = {}
    for e in entry_rows:
        for me, mate in (("player1_id", "player2"), ("player2_id", "player1")):
            if e[me]:
                partner_draw[(e[me], e["tournament_slug"], e["gender"], e["level"])] = e[mate]
    partner_prof = {(r["player_id"], event_key(r["event"]), (r["division"] or "").lower()): r["partner"]
                    for r in season_rows if r["partner"]}
    wl = defaultdict(lambda: [0, 0])
    if not Matches.empty:
        done = Matches[Matches["status"] == "completed"]
        for _, m in done.iterrows():
            for side in (1, 2):
                for k in ("p1", "p2"):
                    pid_ = m[f"team{side}_{k}_id"]
                    if pid_:
                        key = (pid_, m["tournament_slug"], m["gender"], m["level"])
                        wl[key][0 if m["winner"] == side else 1] += 1
    partners, psrc, wins, losses = [], [], [], []
    for _, r in Events.iterrows():
        k_prof = (r["player_id"], event_key(r["event"]), r["division"])
        # gender group matters: Men's 40+ and Mixed Open are both level "open"
        k_draw = (r["player_id"], r["tournament_slug"], r["ranking_category"], r["division"])
        if partner_prof.get(k_prof):
            partners.append(partner_prof[k_prof]); psrc.append("profile")
        elif r["tournament_slug"] and partner_draw.get(k_draw):
            partners.append(partner_draw[k_draw]); psrc.append("draw_entry")
        else:
            partners.append(None); psrc.append(None)
        w = wl.get(k_draw) if r["tournament_slug"] else None
        wins.append(w[0] if w else None)
        losses.append(w[1] if w else None)
    Events["partner"], Events["partner_source"] = partners, psrc
    Events["wins"], Events["losses"] = wins, losses
    prize = {(r["player_id"], event_key(r["event"]), (r["division"] or "").lower()): r["prize"]
             for r in season_rows if r["prize"]}
    Events["prize"] = [prize.get((r["player_id"], event_key(r["event"]), r["division"]))
                       for _, r in Events.iterrows()]

    # ---- ID link validation: a ranked player who played a completed tournament
    # should have a ranking result for that event (brief: consistent ids).
    have_event = set(zip(Events["player_id"], Events["tournament_slug"]))
    link_rows = []
    if not Matches.empty:
        # Mixed divisions do not feed any ranking category, so they are not checked
        # only tournaments that have finished (points are awarded after the final)
        finished = {r["slug"] for r in t_rows if str(r["status"]).lower() == "completed"}
        comp = Matches[(Matches["status"] == "completed") & Matches["tournament_slug"].isin(finished)
                       & (Matches["gender"] != "Mixed")]
        for side in ("team1_p1", "team1_p2", "team2_p1", "team2_p2"):
            for pid_, slug, name, how in zip(comp[f"{side}_id"], comp["tournament_slug"], comp[side],
                                             comp[f"{side}_id_source"]):
                if pid_ in ranked_ids:
                    link_rows.append({"player_id": pid_, "name": name, "tournament_slug": slug,
                                      "id_source": how, "event_result_found": (pid_, slug) in have_event})
    Links = pd.DataFrame(link_rows).drop_duplicates(["player_id", "tournament_slug"]) if link_rows else pd.DataFrame()

    # ---- points check on Players
    counting = (Events[Events["counts_for_ranking"] == True]
                .groupby(["player_id", "ranking_category"])["points"].sum())
    if not Players.empty:
        # a ranked player with no counting results sums to 0, not missing
        Players["sum_counting_points"] = [
            (int(counting.get((r["player_id"], r["category"]), 0)) if isinstance(r["category"], str) else None)
            for _, r in Players.iterrows()]
        Players["points_check"] = [
            (None if pd.isna(r["points"]) else bool(r["sum_counting_points"] == r["points"]))
            for _, r in Players.iterrows()]

    # ---- alias table
    Aliases = pd.DataFrame([{"variant": k, "canonical": v, "canonical_player_id": idx.get(v)}
                            for k, v in NAME_ALIASES.items()])

    # ---- coverage per event
    cov = []
    for (event, slug), grp in Events.groupby(["event", "tournament_slug"], dropna=False):
        n_match = int((Matches["tournament_slug"] == slug).sum()) if isinstance(slug, str) and not Matches.empty else 0
        cov.append({"event": event, "tournament_slug": slug if isinstance(slug, str) else None,
                    "awarded_on": grp["awarded_on"].max(), "event_type": grp["event_type"].iloc[0],
                    "player_rows": len(grp), "matches_available": n_match,
                    "match_source": "tournament page" if n_match else "none on site"})
    Coverage = pd.DataFrame(cov).sort_values("awarded_on", ascending=False)

    def canon_name(pid_):
        return next((r["name"] for r in rank_rows if r["player_id"] == pid_), pid_)

    # ---- ID crosswalk: every id the site uses for a person -> canonical player_id
    xw = []
    for r in rank_rows:
        xw.append({"player_id": r["player_id"], "id": r["player_id"], "id_type": "rankings_profile",
                   "name_on_record": r["name"], "is_guest": None, "context": r["category"]})
    for rec in api_players:
        cid = rec["id"] if rec["id"] in ranked_ids else (resolve_name(rec.get("display_name"), idx)[0] or rec["id"])
        xw.append({"player_id": cid, "id": rec["id"], "id_type": "api_player", "name_on_record": rec.get("display_name"),
                   "is_guest": rec.get("is_guest"), "context": ", ".join(rec.get("events") or [])})
    for e in entry_rows:
        for k in ("1", "2"):
            if e[f"player{k}_entry_id"]:
                xw.append({"player_id": e[f"player{k}_id"], "id": e[f"player{k}_entry_id"], "id_type": "draw_entry",
                           "name_on_record": e[f"player{k}"], "is_guest": None,
                           "context": f"{e['tournament_slug']} / {e['division']}"})
    Crosswalk = (pd.DataFrame(xw).drop_duplicates(["player_id", "id", "id_type", "context"])
                 .sort_values(["player_id", "id_type"]))
    multi = (Crosswalk[Crosswalk["id_type"] != "draw_entry"].groupby("player_id")["id"].nunique())
    for pid_ in multi[multi > 1].index:
        ids = Crosswalk[(Crosswalk["player_id"] == pid_) & (Crosswalk["id_type"] != "draw_entry")]
        issues.append({"type": "person_has_several_ids", "player_id": pid_,
                       "detail": f"{canon_name(pid_)}: " + "; ".join(
                           f"{i} ({t}{', guest' if g is True else ''})"
                           for i, t, g in zip(ids["id"], ids["id_type"], ids["is_guest"]))})

    # ---- display-name variants seen in draws for the same player id
    canon = {r["player_id"]: r["name"] for r in rank_rows}
    canon.update({r["player_id"]: r["name"] for r in player_rows if r["player_id"] not in canon})
    var_rows = Counter()
    if not Matches.empty:
        for side in ("team1_p1", "team1_p2", "team2_p1", "team2_p2"):
            for pid_, n, how in zip(Matches[f"{side}_id"], Matches[side], Matches[f"{side}_id_source"]):
                if isinstance(pid_, str) and isinstance(n, str) and pid_ in canon \
                        and norm_name(n) != norm_name(canon[pid_]):
                    var_rows[(pid_, canon[pid_], n.strip(), how)] += 1
    Variants = pd.DataFrame([{"player_id": k[0], "canonical_name": k[1], "name_in_draw": k[2],
                              "linked_by": k[3], "match_slots": v} for k, v in sorted(var_rows.items())],
                            columns=["player_id", "canonical_name", "name_in_draw", "linked_by", "match_slots"])

    placeholder = sorted({n for n in pd.concat([Entries["player1"], Entries["player2"]]).dropna()
                          if re.search(r"\bpartner\b", n, re.I)}) if not Entries.empty else []
    for n in placeholder:
        issues.append({"type": "placeholder_player_name", "detail": f"draw lists '{n}' (partner not named)"})
    if not CardChecks.empty:
        odd = CardChecks[CardChecks["owner_side_in_schedule"].isna()]
        for pid_, grp in odd.groupby("player_id"):
            issues.append({"type": "profile_shows_other_players_match", "player_id": pid_,
                           "detail": f"{canon.get(pid_, pid_)}: {len(grp)} recent-match card(s) for matches "
                                     f"this player is not listed in (e.g. {grp['card_team1'].iloc[0]}); "
                                     f"likely a substitution or a registration linked to the wrong profile"})

    tables = {
        "Players": Players, "Rankings": Rankings.drop(columns=["category_code"]),
        "Event_Results": Events[["player_id", "player", "ranking_category", "event", "event_canonical",
                                 "event_type", "month",
                                 "awarded_on", "division", "round", "points", "counts_for_ranking",
                                 "partner", "partner_source", "wins", "losses", "prize", "tournament_slug"]],
        "Matches": Matches, "Tournaments": Tournaments, "Divisions": Divisions,
        "Tournament_Entries": Entries, "Group_Standings": Standings, "Player_Cards": Cards,
        "Season_Rows": Season, "Name_Aliases": Aliases, "Unmatched_Names": Unmatched,
        "Match_Coverage": Coverage, "Card_Checks": CardChecks, "ID_Link_Checks": Links,
        "Name_Variants": Variants, "ID_Crosswalk": Crosswalk,
    }
    qa = run_qa(tables, issues)
    tables["QA"] = qa
    tables["Data_Issues"] = pd.DataFrame(issues)
    for name, df in tables.items():
        df.to_csv(OUT / f"{name}.csv", index=False)
    write_excel(tables, OUT / "IPT_Full_Data.xlsx")
    print("\nTable counts:")
    for name, df in tables.items():
        print(f"  {name:20s} {len(df):6d}")
    print("\nQA:")
    for _, r in qa.iterrows():
        print(f"  [{r['result']}] {r['check']}: {r['detail']}")
    return tables


def match_row(m: dict, t: dict, d: dict, team_ids: dict) -> dict:
    segs = m.get("scoreSegments") or []
    s1 = sum(1 for s in segs if (s.get("teamOne") or 0) > (s.get("teamTwo") or 0))
    s2 = sum(1 for s in segs if (s.get("teamTwo") or 0) > (s.get("teamOne") or 0))
    g1 = sum(s.get("teamOne") or 0 for s in segs)
    g2 = sum(s.get("teamTwo") or 0 for s in segs)
    score = ", ".join(
        f"{s.get('teamOne')}-{s.get('teamTwo')}"
        + (f"({s.get('tiebreakTeamOne')}-{s.get('tiebreakTeamTwo')})"
           if s.get("tiebreakTeamOne") is not None else "")
        for s in segs)
    status = (m.get("status") or "").lower()
    winner = {"teamOne": 1, "teamTwo": 2}.get(m.get("winner"))
    t1 = [real_name(x) for x in (m.get("teamOnePlayers") or split_team(m.get("teamOne")))]
    t2 = [real_name(x) for x in (m.get("teamTwoPlayers") or split_team(m.get("teamTwo")))]
    ids1 = team_ids.get(norm_name(m.get("teamOne")))
    ids2 = team_ids.get(norm_name(m.get("teamTwo")))

    def pid(ids, name):
        # map by name within the entry so order differences can't swap ids
        if not ids:
            return None
        if norm_name(ids[2]) == norm_name(name):
            return ids[0]
        if norm_name(ids[3]) == norm_name(name):
            return ids[1]
        return None

    gender, level = division_parts(d.get("name"))
    fmt = m.get("format") or {}
    return {
        "match_id": m.get("id"), "tournament_slug": t["slug"], "event": t.get("title"),
        "event_type": t.get("category"), "division_id": m.get("divisionId") or d.get("id"),
        "division": d.get("name") or m.get("categoryName"), "gender": gender, "level": level,
        "round": m.get("round"), "match_kind": m.get("matchKind"),
        "date": m.get("scheduledDate"), "time": m.get("time"), "court": m.get("court"),
        "started_at": m.get("startedAt"), "ended_at": m.get("endedAt"),
        "team1": m.get("teamOne"), "team2": m.get("teamTwo"),
        "team1_p1": t1[0] if len(t1) > 0 else None, "team1_p2": t1[1] if len(t1) > 1 else None,
        "team2_p1": t2[0] if len(t2) > 0 else None, "team2_p2": t2[1] if len(t2) > 1 else None,
        "team1_p1_entry_id": pid(ids1, t1[0] if t1 else None),
        "team1_p2_entry_id": pid(ids1, t1[1] if len(t1) > 1 else None),
        "team2_p1_entry_id": pid(ids2, t2[0] if t2 else None),
        "team2_p2_entry_id": pid(ids2, t2[1] if len(t2) > 1 else None),
        "score_team1_first": score or None, "sets_team1": s1 if segs else None,
        "sets_team2": s2 if segs else None, "games_team1": g1 if segs else None,
        "games_team2": g2 if segs else None,
        "team1_score": m.get("teamOneScore"), "team2_score": m.get("teamTwoScore"),
        "winner": winner, "result_note": m.get("resultNote"),
        "format": fmt.get("label"), "format_kind": fmt.get("kind"),
        "status": ("completed" if status == "completed"
                   else "scheduled" if status in ("upcoming", "scheduled", "not started") else (status or None)),
        "site_status": m.get("status"),
        "found_on": "tournament_page",
    }


CARD_SCORE_RE = re.compile(r"^\d+-\d+(\(\d+-\d+\))?(, \d+-\d+(\(\d+-\d+\))?)*$")


def card_match_row(cm: dict) -> dict | None:
    """Fallback for a match seen only on a player page card (not in any draw)."""
    lv = cm["leaves"]
    if len(lv) < 6:
        return None
    event, div_round, result, court = lv[0], lv[1], lv[2], lv[3]
    teams = [x for x in lv[4:] if " / " in x][:2]
    score = next((x for x in reversed(lv) if CARD_SCORE_RE.match(x)), None)
    division, _, rnd = div_round.rpartition(" - ")
    sets = [tuple(map(int, re.findall(r"\d+", s)[:2])) for s in score.split(", ")] if score else []
    s1 = sum(a > b for a, b in sets)
    s2 = sum(b > a for a, b in sets)
    slug = None
    if cm.get("href"):
        mm = re.match(r"^/tournaments/([^?]+)", cm["href"])
        slug = mm.group(1) if mm else None
    t1 = split_team(teams[0]) if teams else []
    t2 = split_team(teams[1]) if len(teams) > 1 else []
    gender, level = division_parts(division)
    completed = result in ("Win", "Loss")
    return {
        "match_id": cm["match_id"], "tournament_slug": slug, "event": event, "division": division,
        "gender": gender, "level": level, "round": rnd, "court": court,
        "time": None if completed else result,
        "team1": teams[0] if teams else None, "team2": teams[1] if len(teams) > 1 else None,
        "team1_p1": t1[0] if t1 else None, "team1_p2": t1[1] if len(t1) > 1 else None,
        "team2_p1": t2[0] if t2 else None, "team2_p2": t2[1] if len(t2) > 1 else None,
        "score_team1_first": score, "sets_team1": s1 if sets else None, "sets_team2": s2 if sets else None,
        "games_team1": sum(a for a, _ in sets) if sets else None,
        "games_team2": sum(b for _, b in sets) if sets else None,
        "winner": (1 if s1 > s2 else 2 if s2 > s1 else None) if completed else None,
        "status": "completed" if completed else "scheduled",
        "found_on": f"player_page:{cm['player_id']}",
    }


# ---------------------------------------------------------------- QA

def run_qa(t: dict, issues: list) -> pd.DataFrame:
    res = []

    def add(check, ok, detail):
        res.append({"check": check, "result": "PASS" if ok else "FAIL", "detail": detail})

    P, R, E, M = t["Players"], t["Rankings"], t["Event_Results"], t["Matches"]

    # 1. points reconcile per rankings row (the rank of record)
    s = (E[E["counts_for_ranking"] == True].groupby(["player_id", "ranking_category"])["points"].sum())
    bad = [(r["name"], r["category"], r["points"], s.get((r["player_id"], r["category"]), 0))
           for _, r in R.iterrows() if s.get((r["player_id"], r["category"]), 0) != r["points"]]
    add("Rankings points = sum of counting event points", not bad,
        f"{len(R) - len(bad)}/{len(R)} rows reconcile" + (f"; mismatches: {bad[:10]}" if bad else ""))
    if not P.empty:
        pc = P[P["points_check"] == False]
        add("Players points = sum of counting event points", pc.empty,
            f"{int((P['points_check'] == True).sum())} pass, {len(pc)} fail"
            + (f": {list(pc['name'])[:10]}" if len(pc) else ""))
        mm = P[P["profile_mismatch"] == True]
        add("Profile page agrees with rankings (info)", True,
            f"{len(mm)} players flagged: {list(mm['name'])}" if len(mm) else "none flagged")

    # 2. matches
    if not M.empty:
        ids = M["match_id"]
        add("match_id unique and non-empty", ids.notna().all() and ids.is_unique and (ids != "").all(),
            f"{len(M)} matches, {ids.nunique()} unique, {int(ids.isna().sum())} empty")
        comp = M[M["status"] == "completed"]
        nowin = comp[~comp["winner"].isin([1, 2])]
        add("every completed match has winner 1 or 2", nowin.empty,
            f"{len(comp)} completed, {len(nowin)} without winner"
            + (f": {list(nowin['match_id'])[:5]}" if len(nowin) else ""))
        scored = comp[comp["sets_team1"].notna() & (comp["sets_team1"] != comp["sets_team2"])]
        disagree = scored[((scored["sets_team1"] > scored["sets_team2"]).map(lambda x: 1 if x else 2)) != scored["winner"]]
        wo = comp[comp["sets_team1"].isna() | (comp["score_team1_first"].isna())]
        add("set counts agree with winner", disagree.empty,
            f"{len(scored)} scored matches checked, {len(disagree)} disagree"
            + (f": {list(disagree['match_id'])[:5]}" if len(disagree) else "")
            + f"; {len(wo)} completed without a score (walkovers)")
        if "team1_score" in M:
            ts = comp[comp["team1_score"].notna() & comp["sets_team1"].notna()]
            def agrees(r):
                # race formats: one number per side; set formats: games per set, space separated
                sets = [re.findall(r"\d+", x)[:2] for x in str(r["score_team1_first"]).split(", ")]
                a = [int(x[0]) for x in sets]
                b = [int(x[1]) for x in sets]
                sa = [int(x) for x in str(r["team1_score"]).split()]
                sb = [int(x) for x in str(r["team2_score"]).split()]
                return (sa, sb) == (a, b) or (sa, sb) == ([int(r["sets_team1"])], [int(r["sets_team2"])])
            ok = ts.apply(agrees, axis=1)
            add("summary score agrees with score segments", bool(ok.all()) if len(ok) else True,
                f"{int(ok.sum())}/{len(ok)} agree" + (f": {list(ts[~ok]['match_id'])[:5]}" if len(ok) and not ok.all() else ""))
        n_ids = sum(int(M[f"{s}_id"].notna().sum()) for s in ("team1_p1", "team1_p2", "team2_p1", "team2_p2"))
        n_names = sum(int(M[s].notna().sum()) for s in ("team1_p1", "team1_p2", "team2_p1", "team2_p2"))
        res.append({"check": "match players resolved to player_id",
                    "result": "PASS" if n_ids == n_names else "WARN",
                    "detail": f"{n_ids}/{n_names} player slots have an id; the rest appear nowhere else on "
                              f"the site and are listed in Unmatched_Names"})

    lk = t.get("ID_Link_Checks")
    if lk is not None and not lk.empty:
        miss = lk[~lk["event_result_found"]]
        add("ranked players in completed draws have a ranking result for that event", miss.empty,
            f"{len(lk) - len(miss)}/{len(lk)} player-tournament links confirmed by rankings"
            + (f"; not confirmed: {list(zip(miss['name'], miss['tournament_slug']))[:15]}" if len(miss) else ""))
        by = lk.groupby("id_source").size().to_dict()
        add("how match players were linked to ids (info)", True, str(by))

    cc = t.get("Card_Checks")
    if cc is not None and not cc.empty:
        add("player-page match cards agree with tournament schedule", bool(cc["label_agrees"].all()),
            f"{len(cc)} cards on known matches: {int(cc['same_order'].sum())} list teams in schedule order, "
            f"{int(cc['owner_first_on_card'].sum())} put the profile owner first, "
            f"{int((~cc['label_agrees']).sum())} Win/Loss labels disagree with the schedule winner")

    # 3. rankings categories and contiguity
    cats = set(R["category"])
    add("all three ranking categories present", cats >= set(CATEGORY_LABEL.values()), f"{sorted(cats)}")
    gaps = []
    for c, g in R.groupby("category"):
        rk = list(g.sort_values("rank")["rank"])
        for i, r in enumerate(rk):
            if not (r == i + 1 or (i and r == rk[i - 1])):
                gaps.append((c, i + 1, r))
                break
    add("ranks contiguous apart from ties (competition ranking)", not gaps, f"breaks: {gaps}" if gaps else "ok")

    # 5. spot check against values quoted in the brief (Krishiv Patel profile)
    brief = {"rank": 3, "points": 2460, "win_pct": 83, "rank_move": 1, "career_matches": 6, "wins": 5,
             "losses": 1, "last30_wins": 5, "last30_matches": 6, "career_points": 2820, "titles": 3,
             "runner_up": 2, "defending_points": 2460, "best_rank": 1, "current_streak": 0,
             "longest_streak": 5, "events": 10}
    kp = P[P["name"] == "Krishiv Patel"] if not P.empty else P
    if len(kp):
        row = kp.iloc[0]
        diffs = {k: (v, row[k]) for k, v in brief.items() if num(row[k]) != v}
        add("spot check: Krishiv Patel vs values quoted in brief", not diffs,
            f"{len(brief) - len(diffs)}/{len(brief)} fields match" + (f"; differ (brief, scraped): {diffs}" if diffs else "")
            + ". IPT_Scraped_Data.xlsx was not available, so the other ground-truth players are unchecked")

    # 4. coverage
    have = set(P["player_id"]) if not P.empty else set()
    missing = R[~R["player_id"].isin(have)]
    add("every ranked player has a Players row", missing.empty,
        f"{R['player_id'].nunique() - missing['player_id'].nunique()}/{R['player_id'].nunique()} covered"
        + (f"; missing: {list(missing['name'])[:20]}" if len(missing) else ""))
    add("ID forms (info)", True, str(P["id_form"].value_counts().to_dict()) if not P.empty else "no players")
    return pd.DataFrame(res)


# ---------------------------------------------------------------- excel

def write_excel(tables: dict, path: Path):
    from openpyxl.utils import get_column_letter
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        order = ["QA", "Players", "Rankings", "Event_Results", "Matches", "Tournaments", "Divisions",
                 "Tournament_Entries", "Group_Standings", "Player_Cards", "Season_Rows",
                 "Match_Coverage", "Card_Checks", "ID_Link_Checks", "ID_Crosswalk", "Name_Variants", "Name_Aliases", "Unmatched_Names", "Data_Issues"]
        for name in order:
            df = tables[name]
            (df if not df.empty else pd.DataFrame({"note": ["no rows"]})).to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns if not df.empty else ["note"], 1):
                vals = [str(col)] + [str(v) for v in (df[col].head(200) if not df.empty else [])]
                ws.column_dimensions[get_column_letter(i)].width = min(60, max(8, max(len(v) for v in vals) + 2))
            if not df.empty:
                ws.auto_filter.ref = ws.dimensions


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="visit at most N player pages (smoke test)")
    ap.add_argument("--parse-only", action="store_true", help="skip crawling, rebuild tables from raw/")
    ap.add_argument("--refresh", action="store_true", help="refetch pages even if cached")
    ap.add_argument("--refetch", nargs="+", metavar="URL",
                    help="refetch only these URLs (e.g. one live tournament), then rebuild")
    a = ap.parse_args()
    cache = Cache(refresh=a.refresh)
    if a.refetch:
        cache.refresh = True
        for url in a.refetch:
            print(f"refetching {url}: {'ok' if cache.get(url) else 'FAILED'}")
        cache.refresh = False
        cache.save()
    elif not a.parse_only:
        crawl(cache, a.limit)
    build(cache)


if __name__ == "__main__":
    main()
