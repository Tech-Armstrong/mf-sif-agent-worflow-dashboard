"""Load factsheets already parsed in Historical-Scheme-Portfolio into state/factsheets.json (once).

Manager tenures in the merged JSON are worked out from every factsheet so far. This brings in the ones parsed
before the app existed, so tenures don't start from the first upload:

    python -m scripts.seed_factsheets "C:/.../Historical-Scheme-Portfolio/excel/factsheets/output"
    python -m scripts.seed_factsheets --sif "C:/.../sif-parser/factsheets/output"     # SIF -> state/sif_factsheets.json

It reads output/<YEAR>/<MON>/<AMC or SIF>/fund_details.csv, plus factsheet_manager_exits.csv for exit dates. Factsheets
the app has parsed itself are kept: a seed entry never replaces one.
"""
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline import blob_store, parse_jobs


def main(out_dir: str, fund: str = "MF") -> None:
    out = Path(out_dir)
    entries = {}
    for f in sorted(out.glob("*/*/*/fund_details.csv")):
        with open(f, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if rows:
            year, mon, amc = f.parts[-4], f.parts[-3], f.parts[-2]
            entries[f"{year}/{mon}/{amc}"] = {"source": None, "seeded_from": "sif-parser" if fund == "SIF" else "Historical-Scheme-Portfolio",
                                              "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                              "rows": rows, "exits": []}
    exits_file = out / "factsheet_manager_exits.csv"
    if exits_file.is_file():
        with open(exits_file, encoding="utf-8-sig", newline="") as fh:
            entries["_seed_exits"] = {"source": None, "rows": [], "exits": list(csv.DictReader(fh))}

    def change(state):
        state = dict(state or {})
        added = 0
        for k, v in entries.items():
            if k not in state or state[k].get("seeded_from") or k == "_seed_exits":
                state[k] = v
                added += 1
        print(f"{added} factsheet entries written, {len(state)} in all ({blob_store.enabled_backend()} storage)")
        return state

    blob_store.update_json(parse_jobs.fs_state(fund), change)


if __name__ == "__main__":
    args = sys.argv[1:]
    sif = "--sif" in args
    args = [a for a in args if a != "--sif"]
    if len(args) != 1:
        sys.exit(__doc__)
    main(args[0], "SIF" if sif else "MF")
