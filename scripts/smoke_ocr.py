"""Exercise real production OCR on a synthetic Chinese image-only PDF."""
import argparse
import json
from pathlib import Path

from litworkbench.demo import make_fixtures
from litworkbench.ingest import import_document, search
from litworkbench.store import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    fixtures = make_fixtures(args.workdir / "synthetic", font=args.font)
    workspace = Workspace(args.workdir / "library")
    if not workspace.exists():
        workspace.initialize()
    document = import_document(workspace, fixtures["scan_pdf"], ocr_mode="force")
    assert document["status"] == "completed", document
    unit = workspace.units_for_doc(document["id"])[0]
    assert unit["method"] == "ocr" and unit["file_page"] == 1
    assert "白塔" in unit["original_text"] and search(workspace, "白塔")
    data = unit["ocr_json"]
    if isinstance(data, str):
        data = json.loads(data)
    assert data["render_width"] > 0 and data["render_height"] > 0
    assert data["lines"] and all(len(line["box"]) == 4 for line in data["lines"])
    print(json.dumps({"passed": True, "synthetic": True, "engine": "rapidocr", "unit_id": unit["id"], "text": unit["original_text"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
