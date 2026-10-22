# Local Literature Workbench Implementation Plan

Implementation completed and independently accepted on 2026-10-12. The checklist below records the original plan; the actual commands, results, and remaining product boundaries are in `verification.md`.

> For agentic workers: implementation is delegated to OpenCode under the user's global instructions. Codex owns scope, independent testing, review, and acceptance. Use test-first implementation in coherent feature clusters. Do not commit, push, or delegate back.

**Goal:** Deliver a working local CLI that imports PDF/DOCX, preserves provenance, searches Chinese text, curates evidence, and runs resumable API-backed research to cited exports.

**Architecture:** Python package with argparse CLI, SQLite repository, page/paragraph ingestion, optional CPU OCR, explicit evidence validation and a bounded JSON-action agent loop. Use immutable copied sources, immutable extraction text, revision records and snapshot-based tasks.

**Tech Stack:** Python >=3.11 (verification uses 3.12), sqlite3, argparse, tomllib, pypdfium2, Pillow, python-docx, httpx; optional rapidocr + onnxruntime; pytest + reportlab for development fixtures. Use uv and a separate virtual environment.

**Spec:** `docs/design.md` (user confirmation and implementation decisions).

## Global Constraints

- Work only in the supplied absolute project directory; temporary data under the supplied sibling `work` directory. Do not use system/user credentials or real external AI during tests.
- Local first. No web service, GUI, telemetry, authentication, vector database, or model training in this version.
- PDF and DOCX originals preserved; old DOC rejected with conversion guidance. Extractions and generated analysis are distinct.
- UTF-8 JSON output; data on stdout and progress/errors on stderr; useful nonzero exit for errors. Global `--workspace PATH`, all examples use it before the subcommand.
- Secrets referenced by environment variable; no secrets in config dumps, task histories, error bodies, or exports.
- Use a standard pyproject with console script `lit = litworkbench.cli:main` and `python -m litworkbench`.
- Model actions validate arguments and never run shell. A failed tool action is feedback the agent can correct, not task success.
- Keep modules focused; do not implement a single huge CLI file. No git commits or system-wide installations.

## Review Focus

1. A scan with only a selectable page number/header must enter OCR; force OCR exists for bad longer text layers.
2. Duplicate/failed imports must not silently become completed or duplicate evidence; partial data stays diagnosable and retryable.
3. One- and two-character Chinese terms, punctuation, SQL `%`/`_`, and Chinese broken over line endings must search correctly.
4. A fabricated quote, unknown/cross-task evidence ID, rejected evidence or empty citation list cannot support a finding.
5. Timeout, malformed model JSON, step exhaustion, interruption and corrected source text preserve recovery state and revision provenance without leaking secrets.

### Task 1: Workspace, document import and CLI foundation

**Files:** `pyproject.toml`, `src/litworkbench/{__init__,__main__,cli,config,store,ingest,ocr}.py`, `src/litworkbench/profiles/{general,heritage}.toml`, `tests/{conftest,test_ingest,test_search}.py`.

**Interfaces:** `Workspace(root: Path)` owns config, SQLite, sources and artifacts. `import_document(workspace, path, ocr_mode='auto') -> dict`; `search(workspace, query, limit=20) -> list[dict]`; `read_unit(workspace, unit_id) -> dict`. CLI init/import/list/search/read/render/doctor/metadata/label/correct commands return serializable data. CLI and Agent use the same functions.

- [ ] First add failing tests for init refusing overwrite, text PDF import, ordered DOCX paragraph/table import, duplicate hash, per-page OCR decision including header-only scan, short Chinese/literal punctuation search and retrying pending OCR.
- [ ] Implement schema/config, immutable source copies and per-unit persistence with explicit pending/failed/completed states. PDF page index starts at 1. OCR engine initializes lazily once per batch and retains actual boxes/confidence, not dummy output.
- [ ] Implement CLI commands and Chinese-friendly diagnostics. Directory import supports a small supported extension allowlist, does not ingest its own workspace recursively.
- [ ] Run `python -m pytest tests/test_ingest.py tests/test_search.py -q` and `python -m litworkbench --help`.

### Task 2: Evidence, correction and export

**Files:** `src/litworkbench/{evidence,export}.py`, additions to CLI/store, `tests/{test_evidence,test_export}.py`.

**Interfaces:** `save_evidence(workspace, unit_id, quote, category, note='', task_id=None) -> dict`; `correct_unit(workspace, unit_id, text, reason) -> dict`; `export_evidence(...)`; `export_report(workspace, task_id, output: Path, format: str) -> Path`.

- [ ] Add failing tests for false quote rejection, exact offsets, original/corrected version snapshots, stale evidence review flags after correction, and rejected evidence exclusions.
- [ ] Implement manual excerpt creation/list/review and correction with audit history. Bibliography edit and printed-page label change must not alter source text or file page index.
- [ ] Add failing tests that exported Markdown/JSON/DOCX contain source locator, quotes, categories and verification state. Evidence CSV must guard leading spreadsheet formula characters.
- [ ] Implement exports using stored authoritative source metadata, not model-invented citations. Escape Markdown/XML via the appropriate library. No reports claimed as fully human verified.
- [ ] Run `python -m pytest tests/test_evidence.py tests/test_export.py -q`.

### Task 3: Configurable resumable research agent

**Files:** `src/litworkbench/{provider,agent}.py`, CLI research/resume/tasks/export, `tests/{test_agent,test_provider}.py`.

**Interfaces:** `ChatProvider(config).complete(messages: list[dict]) -> str` sends an HTTP chat/completions request with configured model and env key. `run_research(workspace, question, profile, resume_id=None) -> dict` checkpoints every iteration.

- [ ] Use test-first local fake HTTP provider returning a scripted series of JSON actions: search -> read -> excerpt -> finish. This fixture belongs only in tests, not production.
- [ ] Protocol is JSON `{ "tool": "search|read|excerpt|finish", "args": {...} }`; allow one enclosing Markdown JSON fence, reject arbitrary text/extra execution. Tool schemas and config/profile appear in system prompt. Documents explicitly untrusted as instructions.
- [ ] `finish` supplies title, findings `[{text, evidence_ids, section}]` and open_questions. Validate every nonempty finding has existing current-task non-rejected evidence, and only excerpt from read units. Unknown tool/invalid args/quote gets recoverable error feedback, bounded by max_steps.
- [ ] Persist question, profile/config snapshot (no key), messages, actions, evidence, status, report, and total progress. Resume paused/failed/interrupted tasks with a new bounded step budget, without duplicating committed evidence. Completed tasks should be inspectable, not silently rerun.
- [ ] Missing provider config fails clearly without network; HTTP/network errors become sanitized failures; no raw headers or response body containing secrets. KeyboardInterrupt records interruption. No successful report on step exhaustion.
- [ ] Tests cover budget pause then resume, timeout then resume, invalid JSON, unknown evidence/cross-task IDs, prompt injection action rejection, missing key, key not in persisted files, and no-evidence report.
- [ ] Run `python -m pytest tests/test_agent.py tests/test_provider.py -q`.

### Task 4: Documentation and runnable acceptance

**Files:** `README.md` (Chinese), `examples/` (safe config/profile only), `scripts/make_demo.py`, `scripts/smoke_ocr.py`, complete tests.

- [ ] README contains installation, actual CLI help-derived commands, API configuration, local compatible server example, offline operation, troubleshooting, supported/unsupported types, and explicit limitations. No claim of actual API or real gazetteer validation.
- [ ] Add clearly synthetic fixture generator (never represent sample text as an actual county gazetteer). Generate selectable PDF, Chinese image-only PDF, mixed PDF, and ordered DOCX into a chosen work directory. Use a detected CJK font for the raster fixture; report skip only if absent.
- [ ] `scripts/smoke_ocr.py --workdir PATH` runs actual OCR on a synthetic Chinese scan and asserts a known phrase plus provenance/boxes. It must exercise production import, not call a mocked OCR service.
- [ ] Run full `python -m pytest -q`, `python -m compileall -q src`, `python -m litworkbench --help`, `python scripts/smoke_ocr.py --workdir <scratch>` and report commands/output, changed files, unresolved limitations.

## Environment for this run

- Project: `C:\Users\杨睿\Documents\Codex\2026-10-11\w-2\outputs\litworkbench`
- Python: `C:\Users\杨睿\Documents\Codex\2026-10-11\w-2\work\litworkbench-venv\Scripts\python.exe`
- uv: `C:\Users\杨睿\Documents\Codex\2026-10-11\w-2\work\tools\uv\uv-0.13.0.data\scripts\uv.exe`
- Install: `& '<uv>' pip install --python '<Python>' -e '.[dev,ocr]'`
- Do not wait for user review; the user explicitly said to start. Codex reviews and accepts after implementation.
