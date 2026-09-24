# IPT scraper: run notes

Run date: 22 Sep 2026. Source: indianpadeltour.in (plus its public API at api.indianpadeltour.in).
Output: `ipt_data/IPT_Full_Data.xlsx` and one CSV per sheet. QA: 16 of 16 checks pass (see the QA sheet).

## How to rerun

```
pip install pandas openpyxl
python ipt_scraper.py                 # crawl (skips cached pages) + build
python ipt_scraper.py --parse-only    # rebuild tables from raw/ only
python ipt_scraper.py --refresh       # refetch everything
python ipt_scraper.py --limit 5       # smoke test: 5 player pages
```

A full crawl from empty is about 1,200 requests at one request per ~2.5 s (1.5 s delay plus fetch), so roughly 50 minutes. Requests are strictly sequential.

## Changes from the brief

- **No browser.** The brief assumed the pages render client side. They do not: Next.js embeds the full page data as JSON in the HTML (the React Server Components payload in the `self.__next_f.push(...)` script tags). The scraper decodes that payload and reads the JSON directly. Tabs, filters, "load more" and text regexes are not needed. All three ranking categories arrive complete in one page (693 + 166 + 57 rows).
- **Starting files.** `ipt_scraper.py` and `IPT_Scraped_Data.xlsx` were not on this machine, so the scraper was written from scratch. The ground-truth comparison against the xlsx could not be run. Krishiv Patel was checked against the values quoted in the brief (17 of 17 fields match). Aditya Jagtap and Rahul Motwani are unchecked until the xlsx is available.
- **Extra sheets** beyond the schema: Divisions, Tournament_Entries, Group_Standings, Season_Rows, Match_Coverage, Card_Checks, ID_Link_Checks, ID_Crosswalk, Name_Variants, Data_Issues.

## Counts per table

| Table | Rows | Notes |
|---|---|---|
| Players | 1,033 | 896 ranked people + 137 unranked people who appear in draws |
| Rankings | 916 | Men's 693, Women's 166, Men's 40+ 57 (20 people are in both Men's and Men's 40+) |
| Event_Results | 1,550 | every ranking result, including aged-out ones; 22 distinct events |
| Matches | 635 | 442 completed, 193 scheduled (Chandigarh 2.0 draw) |
| Tournaments | 15 | 3 completed, 12 upcoming or announced |
| Divisions | 33 | |
| Tournament_Entries | 366 | teams entered per division, with seeds |
| Group_Standings | 351 | |
| Player_Cards | 896 | from Similar Players carousels |
| Season_Rows | 1,314 | "Tournaments This Season" table on each profile |
| ID_Crosswalk | 2,246 | every id the site uses for a person, mapped to one player_id |
| Data_Issues | 39 | see below |

## Where the data comes from

| Source | What it gives |
|---|---|
| `/players-standings` | Full rankings for all three categories, and for every player every ranking result (event, tier, division, stage, points, date, counts_for_ranking). Event_Results comes from here, so no deduplication of the "Road So Far" HTML is needed. |
| `/tournaments/<slug>` | Tournament metadata, and per division: entries with seeds, groups, group standings, bracket, and the full schedule. Each match has a UUID, round, court, date, time, per-set scores, tiebreaks, winner, result note (Walkover / Retired) and format. |
| `/players/<id>` | The profile header (win %, career matches, W/L, last 30 days, titles, career points), stat cards (runner-up, defending points, best ranking, streaks), Tournaments This Season, Recent Matches cards, Similar Players. |
| `api.indianpadeltour.in/api/tournaments?page=N&limit=100` | 15 tournaments. The /tournaments page shows only 6; the other 9 are future calendar entries. |
| `api.indianpadeltour.in/api/players?page=N&limit=100` | 598 player records. The key field is `member_ids`: every per-tournament registration id that belongs to that player. This is what links draws to players. |

Other endpoints seen: `/api/builder/header`, `/api/highlights?limit=10`, `/api/tournaments/live/summary` (all cosmetic), `/api/schedule-download` (the 2026-27 calendar PDF, 7.6 MB, not downloaded). `/api/rankings` returns 404.

**Selectors used.** These are keys in the embedded JSON, not CSS selectors: `rankings` (standings), `items` (tournament list), `tournament` (tournament page), and on profiles `player`, `rows` (season table), `players` (similar players), plus `article` elements keyed by stat label ("Runner-up", "Best Ranking", "Current Streak" and so on) or by match UUID (recent match cards). The code is in `extract_*` in `ipt_scraper.py`.

## Player IDs (most important for the dashboard)

- **Use `player_id` everywhere.** It is the rankings profile id, the same id as in `/players/<id>`.
- **Draws do not use profile ids.** Every tournament entry has a fresh registration id. For example, Krishiv Patel is `2ebd365e...` at Goa and `ef661582...` at Kochi. Those URLs still open his profile, so a crawler that follows draw links fetches the same people again. The scraper maps registration ids to player_id through the API's `member_ids` (473 player-tournament links) and falls back to exact name only when needed (3 links, all "guest" records). Validation: all 476 links between ranked players and completed events are confirmed by that player's own ranking result for the event.
- **Both id forms.** 448 ranked players have UUIDs and 448 have `ranked-<name>` slugs (no account). Two people have both forms: Jasmer Kapany and Kaustubh Thakur are ranked under a slug but also hold UUID accounts in the API. 11 people have more than one id in total, usually a normal account plus a "guest" account (Krishiv Patel is one). All are listed in ID_Crosswalk and Data_Issues.
- **Unranked draw players** (137) get their API player id as player_id. Their Players rows have profile stats but no rank or points.
- **The brief's name variants** (Paramveer Bajwa, Swaraj Deshmukh, Saish Shelkar) are resolved through the id link, not the alias table. The alias table is kept as a fallback. Name_Variants lists every printed name that differs from the canonical one, including Treta Bhattacharya, Famas Shanavas, Kunal Premnarayan and Sidharth Gupta.

## Known data issues found (all in Data_Issues)

1. **Profile header vs rankings (17 players, not only Samar Malhotra).** On some profiles the header rank and points come from one division record rather than the category ranking. Samar shows "Men's Advance #1, 600" against rankings Men's #2, 3,660. Tulsi Mehta shows "Women's Advance #1, 600" against Women's #5, 1,420. Rankings values are the rank of record. The profile values are kept in `profile_category`, `profile_rank`, `profile_points` and flagged in `profile_mismatch`. All five of Samar's registration ids open the same profile, so there is no second account holding his Men's record.
2. **Samar's "Winner (Open)" at Goa GS 11.0** is his Mixed Open title. The rankings row for Goa is Men's Advance finalist, and the Goa final matches show his pair (with Aryan Hemdev) losing to Aditya Jagtap / Krishiv Patel. The division column separates the two. Mixed Open results are not part of any ranking category.
3. **One id, two names (3 accounts).** The rankings call an account "Ziaan Talab", while the API and the newer Chandigarh draw call the same id "Tushar Jagota". Mayank Daga / Joel Duarte and Madhu Samtani / Aryan Manchanda follow the same pattern. The site links the names, so they are kept as one player_id. Whether this is a rename or a reused account cannot be known from the site.
4. **Swapped ids.** In the Goa Mixed Open entry "Anmol Malhotra / Yugantar Malhotra", the API links each registration to the other person's account. Printed names were trusted. Both players are on the same team, so no match result is affected.
5. **Linked divisions.** Chandigarh 2.0 Women's Advance and Women's Intermediate share one draw, so 15 matches are listed under both. They are deduplicated on match_id, and `listed_under` records both.
6. **Placeholder partners.** 4 entries name the partner as "<Name> Partner", e.g. "Ryan Carroll Partner". They are kept as printed.
7. **Event name variant.** "BENGALURU CITY OPEN 2.0" (Women's, 8 rows) and "BANGALORE CITY OPEN 2.0" (Men's, 40 rows) are the same date and tier. `event_canonical` groups them.
8. **Player-page cards.** Recent-match cards put the profile owner's team first (2,036 of 2,056). Tournament schedules use a fixed team1/team2. The Matches table always uses schedule order. Every card's Win/Loss label agrees with the schedule winner.

## Known gaps

- **Match coverage.** The site hosts draws only for Mumbai City Open 5.0, Goa GS 11.0 and Kochi City Open 2.0 (442 completed matches). The other 19 ranked events (IPT 3.0 to 10.0 and the older City Opens) have ranking results but no match data anywhere on the site. The Match_Coverage sheet shows this per event. Vikram Shah and Arjun Kochhar have 0 matches because they have not played the three hosted events.
- **Partner, W and L per event** are filled for the 478 event rows at those three tournaments (356 from draw entries, 122 from profiles) and left blank for older events. Nothing is guessed. The site's own W, L and Prize columns are empty on every profile.
- **Walkovers.** 23 matches are marked Walkover and 3 Retired. 7 completed walkovers have no score; they carry a winner but no sets.
- **Ranking History chart.** Its data is not in the page payload, and no API route for it was found. It is not scraped.
- **best_rank / current_streak** are shown as "-" on some profiles (132 and 448 missing respectively) and stay blank.
- **Home city** is not published. Regional analysis is not possible from this source.
- **Mixed Open** (Goa only) is in Matches and Tournament_Entries but does not feed any ranking.

## robots.txt and terms

- `robots.txt` returns 404 (none published).
- The Terms and Conditions page (`/content/terms-and-conditions`) is placeholder text ("Detailed terms can be updated from the admin content management module") with no clause on automated access or reuse.
- The crawler identifies as a normal browser, makes one request at a time with a 1.5 s delay, and caches everything so reruns do not touch the site.

## Changes on 24 Sep 2026 (while Chandigarh 2.0 was live)

- New `--refetch URL ...` option refetches only the given pages (for example one live tournament and the API player list) and rebuilds the tables, without a full crawl.
- Knockout slots that are not filled yet ("Group A - 1st", "Winner - QF 1", "Rank 2") are recognised as placeholders, not players. They also describe the bracket, which the Chandigarh simulation uses.
- The Chandigarh draw was republished with new match ids after the player pages were cached. Player-page match cards are now used only for tournaments that have no draw page; 148 stale cards were ignored and logged in Data_Issues.
- A last-resort link from a draw name to an API player record by display name (only where the name is unambiguous).
- The ranked-players check only covers tournaments that have finished (points are awarded after the final).

## Files

- `ipt_scraper.py`: crawler, parser, table builder, QA.
- `raw/pages/*.html.gz` and `raw/index.json`: raw cache of every page and API response (URL, status, time). Never delete; `--parse-only` rebuilds everything from it. `raw/api/` holds two manual API snapshots taken during exploration.
- `ipt_data/`: the deliverable.
- `logs/`: output of both crawl runs and the final build.
