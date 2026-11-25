# sif-parser (copied)

Copied unchanged on 2026-11-25 from sif-parser (`sif-parser-main`). The folder layout is kept because the scripts find
their data relative to their own location. This folder's `README.md` is sif-parser's own.

| Here | Used for |
|---|---|
| `sif_parser.py`, `market_cap_mapping/`, `industry_sector_mapping/`, `scheme_master_mapping/sif_scheme_master.xlsx` | SIF Monthly Portfolio Disclosures |
| `factsheets/factsheet_parser.py`, `fund_extractor.py`, `rules/`, `factsheet_scheme_alias.csv`, `factsheet_manager_alias.csv` | SIF factsheets |
| `factsheets/manager_tenures.py`, `manager_exits.py` | merge, and exit dates (`../factsheet_exits.py <pdf> <in> <out> sif/factsheets`) |
| `merge_portfolio_factsheet.py` | SIF merged JSON |

Not used by the app: `factsheets/factsheet_portfolio.py` (portfolios read from the factsheet), `ingestion-pipeline/` (Neo4j).

`pipeline/parse_jobs.py` runs each parser once per uploaded file, in its own process:

- **Portfolio:** `python sif_parser.py <in> <out>`, with the file at `<in>/<YEAR>/<MON>/<SIF>/<file>`. `<SIF>` is the app's SIF
  name (`pipeline/drive_targets.SIFS`), which is the scheme master's `fund_house`. It reads `holdings.json`, `scheme_snapshot.csv`,
  `amc_reported_totals.csv` and `unclassified_rows.csv` from `<out>`.
- **Factsheet:** runs on a temporary copy of this folder, with the PDF at `data/factsheets/<YEAR>/<MON>/<SIF short> Factsheet.pdf`,
  as `python factsheets/factsheet_parser.py --year <YEAR> --month <MON>`. It reads `factsheets/output/<YEAR>/<MON>/<SIF>/fund_details.csv`.
  The docling and LLM caches are kept in storage, as for MF.
- **Merge:** `python merge_portfolio_factsheet.py --holdings <holdings> --factsheets <every SIF factsheet's rows> --year --month`.

## Keeping it up to date
Copy the changed files over these and push; the parse-jobs workflow rebuilds both job images.

- **New SIF:** add it to `SIFS` in `pipeline/drive_targets.py` under its scheme master `fund_house` name, then rebuild the scheme master
  (`scheme_master_mapping/build_sif_scheme_master.py`).
- **New strategy category:** add it to `SIF_FACTSHEET_CATEGORIES` in `pipeline/parse_jobs.py` as well as to the parser's `CATEGORIES`.
