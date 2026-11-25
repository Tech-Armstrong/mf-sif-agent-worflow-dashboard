# Historical-Scheme-Portfolio parsers (copied)

These files were copied unchanged on 2026-11-24 from the Historical-Scheme-Portfolio repo (`historical-mf`). The folder layout is kept because the scripts find their data relative to their own location.

| Here | From | Used for |
|---|---|---|
| `excel/standardized_format/mf_portfolio_parser.py` | same path | Monthly Portfolio Disclosures |
| `excel/market_cap_mapping/`, `excel/industry_sector_mapping/` | same path | portfolio parser |
| `excel/scheme_master_mapping/scheme_master.xlsx`, `scheme_name_mapping.csv` | same path | both parsers |
| `excel/factsheets/factsheet_parser.py`, `fund_extractor.py`, `rules/`, `factsheet_scheme_alias.csv`, `factsheet_manager_alias.csv` | same path | factsheets |
| `excel/factsheets/manager_tenures.py`, `manager_exits.py` | same path | merge, and exit dates (`../factsheet_exits.py` calls its functions) |
| `excel/merge_portfolio_factsheet.py` | same path | merged JSON |
| `mf_extraction_evaluation/evaluation/` | same path | checks imported by the parsers |

`pipeline/parse_jobs.py` runs each parser once per uploaded file, in its own process, with LangSmith tracing off:

- **Portfolio:** `python mf_portfolio_parser.py <in> <out>`, with the file at `<in>/<YEAR>/<MON>/<AMC>/<file>`. It reads `holdings.json`, `scheme_snapshot.csv` and the check CSVs from `<out>`.
- **Factsheet:** runs on a temporary copy of this folder, with the PDF at `excel/factsheets/<YEAR>/<MON>/<AMC>.pdf`, as `python factsheet_parser.py --year <YEAR> --month <MON>`. It reads `output/<YEAR>/<MON>/<AMC>/fund_details.csv`. The docling `pages/` and LLM `llm/` caches are kept in storage, so a re-parse is cheap. `API_KEY` / `endpoint` / `deployment_name` come from the app's `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `FACTSHEET_DEPLOYMENT` (or `AZURE_OPENAI_DEPLOYMENT`).
- **Merge:** `python merge_portfolio_factsheet.py --holdings <month's holdings> --factsheets <every factsheet's rows> --year --month`.

## Keeping it up to date
When these files change in Historical-Scheme-Portfolio, copy the same files over them and push. The parse-jobs workflow rebuilds the job images.

- **New market-cap list:** the parser needs `market_cap_<June|December>_<YEAR>.xlsx` for the disclosure's half-year. If it's missing, that file fails to parse.
- **New AMC:** the AMC folder name must be a key in `AMC_STD` in `mf_portfolio_parser.py`. If the app's AMC name (in `pipeline/drive_targets.AMCS`) differs, add it to `PARSER_AMC_FOLDER` in `pipeline/parse_jobs.py`.
- **New fund in a category:** add it to `scheme_master.xlsx`. The factsheet parser only keeps funds listed there.
