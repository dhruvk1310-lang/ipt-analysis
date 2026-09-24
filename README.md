# Indian Padel Tour, analysed

Independent analysis of the Indian Padel Tour (indianpadeltour.in), built from the tour's public data.

- **Website:** see the GitHub Pages link in this repo's About panel (served from `docs/`).
- **Full report:** [`report/IPT_Analysis_Report.docx`](report/IPT_Analysis_Report.docx)

## What's in it

1. **Ranking forecast.** When each player's points expire, what the table looks like once they do, and what each top player must earn to hold their rank. Includes the finding that the 2026-27 season pays fewer points per result (a City Open title fell from 400 to 300).
2. **Match model.** A game-level strength model built from ranking points and past games, tested only on tournaments it had never seen. It picks about 69% of winners (a coin flip picks 50%), and its confidence is well calibrated.
3. **City Open Chandigarh 2.0 forecast.** Win chances for every match and title odds for five divisions from 20,000 simulations that follow the site's own group and knockout rules. The forecast was frozen before the matches were played and is scored afterwards.

## Layout

| Path | What it is |
|---|---|
| `ipt_scraper.py` | Scraper: site + public API into `ipt_data/` (CSV and Excel), with QA checks. See `RUN_NOTES.md`. |
| `ipt_data/` | The dataset: players, rankings, event results, matches, tournaments, ID crosswalk, QA. |
| `analysis/common.py` | Shared helpers: loading, ranking-window rules, chart style. |
| `analysis/a1_ranking_forecast.py` | Analysis 1. |
| `analysis/padel_model.py` | The strength model and match-format maths. |
| `analysis/a2_match_model.py` | Analysis 2: backtest and final model. |
| `analysis/a3_chandigarh_forecast.py` | Analysis 3: Chandigarh odds, frozen with a timestamp. |
| `analysis/a4_evaluate_chandigarh.py` | Scores the frozen forecast after the event. |
| `analysis/build_report.py` | Builds the Word report. |
| `analysis/build_site.py` | Builds `docs/data.json` for the website. |
| `outputs/` | Every table (CSV), chart (PNG) and summary (JSON) the report and site use. |
| `docs/` | The website (GitHub Pages). |

## Run it

```bash
pip install -r requirements.txt
./run_all.sh                 # refresh data, rerun analyses, rebuild report and site
OFFLINE=1 ./run_all.sh       # same, without touching the website
```

After Chandigarh finishes (27 Sep 2026):

```bash
python3 analysis/a4_evaluate_chandigarh.py && OFFLINE=1 ./run_all.sh
```

The raw page cache (`raw/`) is not in this repo; running `ipt_scraper.py` rebuilds it (about 50 minutes, one request every 1.5 seconds).

Data from indianpadeltour.in. Not affiliated with the Indian Padel Tour.
