#!/usr/bin/env bash
# Rebuild everything: data, analyses, Word report and website data.
#   ./run_all.sh              refresh data incrementally, rerun analyses, rebuild report + site
#   FREEZE=1 ./run_all.sh     also re-freeze the Chandigarh forecast (only before matches are played!)
#   OFFLINE=1 ./run_all.sh    skip the scraper, use the cached data
set -euo pipefail
cd "$(dirname "$0")"

if [ "${OFFLINE:-0}" != 1 ]; then
  python3 ipt_scraper.py                      # only fetches pages not already in raw/
fi

cd analysis
python3 a1_ranking_forecast.py                # analysis 1: ranking forecast
python3 a2_match_model.py                     # analysis 2: model backtest + final fit
if [ "${FREEZE:-0}" = 1 ]; then
  python3 a3_chandigarh_forecast.py           # analysis 3: freeze Chandigarh predictions
fi
python3 a4_evaluate_chandigarh.py --no-fetch  # scorecard (says "no scored matches yet" until results exist)
python3 build_report.py                       # report/IPT_Analysis_Report.docx
python3 build_site.py                         # docs/data.json for the website
echo "done: open docs/index.html via a local server, or push to GitHub Pages"
