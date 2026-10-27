"""Independent regressions for the reviewed notes transaction boundaries."""

import pytest

from litworkbench import notes
from litworkbench.errors import LitError


def page(workspace, title="before", **properties):
    return notes.create_page(workspace, title=title, parent_id=None,
                             body={"schema_version": 1, "blocks": []},
                             properties=properties, request_id=f"create-{title}")


@pytest.mark.parametrize("kind", ["empty", "body", "blocks"])
def test_successful_noop_is_replayable_after_other_changes(workspace, kind):
    if kind == "blocks":
        original = notes.create_page(workspace, title="before", parent_id=None,
            body={"schema_version": 1, "blocks": [
                {"id": "b1", "type": "paragraph", "props": {}, "content": [], "children": []}
            ]}, properties=None, request_id="create-before")
    else:
        original = page(workspace)
    kwargs = dict(expected_revision=1, request_id="no-op")
    if kind == "blocks":
        call = lambda: notes.edit_page_blocks(workspace, original["id"], operations=[
            {"op": "replace", "block_id": "b1", "block": original["body"]["blocks"][0]}
        ], **kwargs)
    else:
        changes = {} if kind == "empty" else {"body": original["body"]}
        call = lambda: notes.update_page(workspace, original["id"], changes=changes, **kwargs)
    first = call()
    notes.update_page(workspace, original["id"], expected_revision=1,
                       request_id="other", changes={"title": "after"})
    assert call() == first
    with pytest.raises(notes.RequestIdReuseError):
        notes.update_page(workspace, original["id"], expected_revision=2,
                           request_id="no-op", changes={"title": "different"})


def test_page_read_uses_one_revision_snapshot(workspace, monkeypatch):
    original = page(workspace, tags=["old"])
    with workspace.connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original_reader = notes._page_row
    fired = False

    def concurrent_write(conn, page_id, **kwargs):
        nonlocal fired
        row = original_reader(conn, page_id, **kwargs)
        if not fired:
            fired = True
            notes.update_page(workspace, page_id, expected_revision=1,
                               request_id="concurrent", changes={"title": "after", "tags": ["new"]})
        return row

    monkeypatch.setattr(notes, "_page_row", concurrent_write)
    observed = notes.get_page(workspace, original["id"])
    assert (observed["revision"], observed["title"], observed["tags"]) == (1, "before", ["old"])


def test_effectively_archived_content_is_read_only(workspace):
    parent = page(workspace)
    child = notes.create_page(workspace, title="child", parent_id=parent["id"],
                               body=None, properties=None, request_id="child")
    notes.update_page(workspace, parent["id"], expected_revision=1,
                       request_id="archive", changes={"archived": True})
    for changes in ({"title": "changed"}, {"tags": ["changed"]}, {"body": child["body"]}):
        with pytest.raises(LitError):
            notes.update_page(workspace, child["id"], expected_revision=1,
                               request_id=f"forbidden-{next(iter(changes))}", changes=changes)
    with pytest.raises(LitError):
        notes.edit_page_blocks(workspace, child["id"], expected_revision=1,
                                 request_id="forbidden-blocks", operations=[])
    with pytest.raises(LitError):
        notes.restore_page(workspace, child["id"], expected_revision=1,
                             request_id="forbidden-restore", revision=1)
    with pytest.raises(LitError):
        notes.create_page(workspace, title="hidden child", parent_id=parent["id"],
                            body=None, properties=None, request_id="forbidden-child")
    moved = notes.update_page(workspace, child["id"], expected_revision=1,
                               request_id="move-out", changes={"parent_id": None})
    assert moved["effective_archived"] is False
