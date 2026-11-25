"""Build step of factsheet.Dockerfile: convert a blank PDF with a factsheet parser's own docling settings,
so every model it uses is downloaded into the image (the parsers run offline).

    python jobs/warm_docling.py [<factsheets folder>]   # default parsers/historical/excel/factsheets (MF);
                                                        # the Dockerfile runs it again for parsers/sif/factsheets
"""
import sys
import tempfile
from pathlib import Path

import pypdfium2 as pdfium

APP = Path(__file__).resolve().parents[1]
folder = Path(sys.argv[1]) if len(sys.argv) > 1 else APP / "parsers" / "historical" / "excel" / "factsheets"
sys.path.insert(0, str(folder if folder.is_absolute() else APP / folder))
import factsheet_parser  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    pdf = Path(tmp, "blank.pdf")
    doc = pdfium.PdfDocument.new()
    doc.new_page(595, 842)
    doc.save(str(pdf))
    factsheet_parser.converter().convert(str(pdf))
print(f"docling models ready ({folder})")
