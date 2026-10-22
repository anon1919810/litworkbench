"""Source provenance must survive every report format and citation failure."""
import json

import pytest
from docx import Document
from test_export import prepared
from litworkbench.evidence import review_evidence
from litworkbench.export import build_report, export_report, export_evidence
from litworkbench.tasks import get_task, save_task_report
from litworkbench.errors import LitError


@pytest.mark.parametrize("format,suffix", [("markdown", ".md"), ("json", ".json"), ("docx", ".docx")])
def test_full_bibliography_in_every_format(workspace, prepared, tmp_path, format, suffix):
    workspace.update_document(prepared["doc"]["id"], title="合成县志", author="验收编者", year="2026", edition="验收版", volume="测试卷")
    workspace.set_printed_page(prepared["unit"]["id"], "317")
    out = export_report(workspace, prepared["task"]["id"], output=tmp_path / ("report" + suffix), format=format)
    text = "\n".join(p.text for p in Document(out).paragraphs) if format == "docx" else out.read_text(encoding="utf-8")
    for expected in ["合成县志", "验收编者", "2026", "验收版", "测试卷", "317", prepared["unit"]["id"], prepared["doc"]["sha256"]]:
        assert expected in text


@pytest.mark.parametrize("bad_reference", ["missing", "foreign", "stale", "rejected"])
def test_bad_citation_never_verifies_report(workspace, prepared, bad_reference):
    evidence = prepared["evidence"]
    review_evidence(workspace, evidence["id"], "accepted")
    task = get_task(workspace, prepared["task"]["id"])
    if bad_reference == "missing":
        task["report"]["findings"][0]["evidence_ids"].append("ev_missing")
    elif bad_reference == "foreign":
        with workspace.connect() as conn:
            conn.execute("UPDATE evidence SET task_id=NULL WHERE id=?", (evidence["id"],))
    else:
        review_evidence(workspace, evidence["id"], bad_reference)
    report = build_report(workspace, task)
    assert report["evidence_verified"] is False
    assert report["conclusions_verified"] is False
    assert report["findings"][0]["supported"] is False


def test_wrong_report_extension_is_rejected(workspace, prepared, tmp_path):
    with pytest.raises(LitError):
        export_report(workspace, prepared["task"]["id"], output=tmp_path / "report.docx", format="json")
    with pytest.raises(LitError):
        export_evidence(workspace, output=tmp_path / "evidence.csv", format="json")


def test_export_does_not_stop_at_first_evidence_page(workspace, prepared, tmp_path):
    with workspace.connect() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(evidence)")]
        original = dict(conn.execute("SELECT * FROM evidence WHERE id=?", (prepared["evidence"]["id"],)).fetchone())
        records = []
        for index in range(5010):
            record = {**original, "id": f"ev_bulk_{index:05d}"}
            records.append([record[c] for c in columns])
        conn.executemany(f"INSERT INTO evidence ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", records)
    out = export_evidence(workspace, output=tmp_path / "all.json", format="json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["count"] == 5011
