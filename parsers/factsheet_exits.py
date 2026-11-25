"""Printed manager exit dates in ONE factsheet, using manager_exits.py's own patterns.

manager_exits.py reads every factsheet PDF ever parsed. The parse job has only the one just uploaded, so this
runs the same search on that PDF and the result is kept with it (state/factsheets.json); the merge joins them.

    python factsheet_exits.py <pdf> <in.json> <out.json> [<factsheets folder>]

The factsheets folder holds the parser's manager_exits.py: historical/excel/factsheets (MF, the default) or
sif/factsheets (SIF).

in.json: {"year": "2026", "rows": [fund_details rows of this PDF], "managers": {scheme_code: [every manager
the fund has listed in any factsheet]}}. out.json: rows in factsheet_manager_exits.csv's columns.
"""
import json
import sys
from pathlib import Path

_FS_DIR = sys.argv[4] if len(sys.argv) > 4 else Path(__file__).resolve().parent / "historical" / "excel" / "factsheets"
sys.path.insert(0, str(_FS_DIR))

import pypdfium2 as pdfium  # noqa: E402
from manager_exits import flatten, printed_exits  # noqa: E402
from manager_tenures import EXCLUDED_CATEGORIES  # noqa: E402


def main(pdf, inp, outp):
    data = json.loads(Path(inp).read_text(encoding="utf-8"))
    rows = [r for r in data["rows"] if r.get("category") not in EXCLUDED_CATEGORIES and r.get("fund_manager") and r.get("pdf_pages")]
    doc = pdfium.PdfDocument(str(pdf))
    texts = [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    doc.close()

    exits, seen = [], set()
    for r in {(r["scheme_code"], r["pdf_pages"]): r for r in rows}.values():
        pages = [int(float(q)) for q in str(r["pdf_pages"]).split(",") if q.strip()]
        text = flatten(" ".join(texts[q - 1] for q in pages if 0 < q <= len(texts)))
        for name in sorted(set(data["managers"].get(r["scheme_code"], [])) | {x["fund_manager"] for x in rows if x["scheme_code"] == r["scheme_code"]}):
            for date, printed in printed_exits(name, text, str(data["year"])):
                if (r["scheme_code"], name, date) not in seen:
                    seen.add((r["scheme_code"], name, date))
                    exits.append({"scheme_code": r["scheme_code"], "scheme_name": r["scheme_name"], "fund_manager": name,
                                  "exit_date": date, "factsheet_month": r["factsheet_month"], "pdf_pages": r["pdf_pages"],
                                  "as_printed": printed})
    Path(outp).write_text(json.dumps(exits, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:4])
