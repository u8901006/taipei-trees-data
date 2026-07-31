# Taipei Trees GitHub Actions Data Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可獨立運作、可稽核、具人工複核護欄的 `taipei-trees-data` GitHub Actions 自動化資料管線。

**Architecture:** Python 3.12 CLI 腳本負責擷取、正規化、異常偵測、會議檔案下載、LLM 結構化抽取、來源健康檢查與缺口報告。GitHub Actions 僅負責排程、權限、提交與開 issue/PR；所有核心邏輯均可在本機以 pytest 測試。原始資料採日期分區且不可覆寫，processed/reports 為可重建衍生物。

**Tech Stack:** Python 3.12、httpx、pandas、pyarrow、beautifulsoup4、pypdf、Anthropic SDK、SQLAlchemy、pytest、GitHub Actions。

## Global Constraints

- 所有 cron 使用 UTC，並精確對應：`0 19 * * *`、`0 1 * * *`、`0 0 * * 1`、`0 2 5,20 * *`、`0 2 10,25 1,4,7,10 *`、`0 2 1 * *`。
- 原始快照 gzip 壓縮、只增不改；同一路徑已存在且內容不同時必須失敗。
- LLM 每個欄位輸出 `{value, page, quote_snippet, confidence}`；無可定位頁碼時 `value` 必須為 `null` 並寫入 `extraction_failures.json`。
- LLM 抽取結果只能經 Pull Request 進入預設分支，不得由 workflow 直接提交。
- 推測移除事件一律標示 `confidence: inferred`，不得當作已證實事實。
- 來源失效時 `reports/health.json` 必須保留 `unavailable_since`，不得靜默沿用舊資料。
- Secrets 僅用於 `DATABASE_URL`、`ANTHROPIC_API_KEY`、`NOTIFY_WEBHOOK`；資料集 ID 與 reviewer 使用 repository variables。
- 所有 workflow 必須有 `workflow_dispatch`、最小權限、timeout；資料寫入 workflow 使用不取消中的 concurrency。
- 缺少選用 secret 時安全跳過對應整合；不得把 secret、連線字串或 API 回應中的敏感標頭寫入 log。
- 生產程式遵守 TDD：先看見測試因缺少行為而失敗，再寫最小實作並重跑。

---

### Task 1: Repository Scaffold and Source Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `scripts/requirements.txt`
- Create: `scripts/__init__.py`
- Create: `scripts/config.py`
- Create: `config/sources.json`
- Create: `tests/test_config.py`
- Create: `.gitignore`
- Create: `README.md`

**Interfaces:**
- Produces: `SourceConfig(name: str, url: str | None, dataset_id: str | None, required: bool)`。
- Produces: `load_sources(path: Path, env: Mapping[str, str]) -> dict[str, SourceConfig]`。
- 預設行道樹資料集 ID：`7a49d00c-a5ff-4a6b-be9e-aaa6dc1ff7e8`。
- 預設會議清單 URL：`https://culture.gov.taipei/News.aspx?n=22311D615C1DFA8E&sms=C4203F8E019F7B1B`。

- [ ] **Step 1: Write failing configuration tests**

```python
def test_environment_overrides_dataset_id(tmp_path, monkeypatch):
    path = write_sources(tmp_path, {"street_trees": {"dataset_id": "default", "required": True}})
    sources = load_sources(path, {"TAIPEI_STREET_TREES_ID": "override"})
    assert sources["street_trees"].dataset_id == "override"

def test_optional_missing_source_is_explicit(tmp_path):
    path = write_sources(tmp_path, {"protected_trees": {"url": None, "required": False}})
    sources = load_sources(path, {})
    assert sources["protected_trees"].available is False
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_config.py -q`
Expected: FAIL because `scripts.config` does not exist.

- [ ] **Step 3: Implement typed source loading and project metadata**

Implement immutable dataclass validation, environment override mapping, pinned-compatible dependencies, directory conventions, setup instructions, repository variables/secrets table, local commands, and data licensing notes. Configuration must explicitly include street trees, protected trees, pruning schedule, review records, and committee records; unknown or unavailable optional sources remain visible.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml scripts config tests .gitignore README.md
git commit -m "chore: scaffold auditable data pipeline"
```

### Task 2: Immutable Open Data Fetching

**Files:**
- Create: `scripts/io_utils.py`
- Create: `scripts/taipei_api.py`
- Create: `scripts/fetch_opendata.py`
- Create: `tests/fixtures/street_trees.csv`
- Create: `tests/test_fetch_opendata.py`

**Interfaces:**
- Produces: `atomic_write_immutable(path: Path, content: bytes) -> Literal["created", "unchanged"]`。
- Produces: `resolve_dataset_resources(dataset_id: str, client: httpx.Client) -> list[Resource]`。
- Produces: `fetch_dataset(source, out_dir, snapshot_date, client) -> FetchResult`。
- CLI: `python scripts/fetch_opendata.py --out raw/ [--date YYYY-MM-DD]`。
- GitHub outputs: `changed`, `fetched_count`, `skipped_sources`。

- [ ] **Step 1: Write failing fetch tests**

Cover: CSV bytes are stored as deterministic gzip; repeat run returns unchanged; differing bytes at an existing snapshot path raise `ImmutableSnapshotError`; correct street-tree CSV resource is selected; an unavailable optional protected-tree source is reported rather than invented.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_fetch_opendata.py -q`
Expected: FAIL because fetch modules do not exist.

- [ ] **Step 3: Implement minimal fetcher**

Use timeout 30 seconds, redirects enabled, retry only transient HTTP status, streamed download size cap, deterministic gzip `mtime=0`, SHA-256 sidecar manifest, and safe filenames. Do not transform raw CSV bytes before compression.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_fetch_opendata.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts tests
git commit -m "feat: add immutable Taipei open-data snapshots"
```

### Task 3: Normalization, Diffing, and Anomaly Detection

**Files:**
- Create: `scripts/normalize.py`
- Create: `scripts/detect_anomalies.py`
- Create: `tests/test_normalize.py`
- Create: `tests/test_anomalies.py`

**Interfaces:**
- Produces: canonical Parquet columns `tree_id`, `district`, `location`, `location_note`, `species`, `diameter_cm`, `height_m`, `survey_date`, `twd97_x`, `twd97_y`, `updated_at`, `source`, `snapshot_date`。
- CLI: `python scripts/normalize.py --raw raw/ --out processed/`。
- CLI: `python scripts/detect_anomalies.py --processed processed/ --out reports/anomalies.json`。
- GitHub output: `found=true|false`。

- [ ] **Step 1: Write failing normalization and anomaly tests**

Cover Big5/UTF-8 CSV decoding, field aliases, numeric coercion, deterministic ordering, duplicate tree IDs, daily count drop strictly over `0.5%`, disappeared IDs, protected-tree deletion severity `critical`, schema change severity `medium`, three unchanged snapshots severity `low`, and inferred removal events.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_normalize.py tests/test_anomalies.py -q`
Expected: FAIL because normalization and anomaly modules do not exist.

- [ ] **Step 3: Implement canonicalization and comparisons**

Write `processed/trees.parquet`, `processed/protected_trees.parquet` when available, `processed/tree_events.jsonl`, and deterministic `reports/anomalies.json` with `summary`, `detail`, `found`, `anomalies`, and `generated_at`. Each disappeared ID event must include `event_type: removal`, `confidence: inferred`, previous and current snapshot dates.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_normalize.py tests/test_anomalies.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts tests
git commit -m "feat: normalize trees and detect public-data anomalies"
```

### Task 4: Optional PostGIS Loader

**Files:**
- Create: `scripts/load_postgis.py`
- Create: `tests/test_load_postgis.py`

**Interfaces:**
- Produces: `load_trees(database_url: str, parquet_path: Path, engine_factory=create_engine) -> LoadStats`。
- CLI: `python scripts/load_postgis.py --src processed/` reads `DATABASE_URL` and exits 0 with an explicit skip message when absent.

- [ ] **Step 1: Write failing loader tests**

Cover missing secret skip, transaction rollback, parameterized upsert keyed by `(source, tree_id)`, and no database URL in log output.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_load_postgis.py -q`
Expected: FAIL because loader does not exist.

- [ ] **Step 3: Implement transactional loader**

Create schema/table/index if needed, use a staging table and one atomic upsert, store TWD97 coordinates without pretending they are WGS84, and return inserted/updated counts.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_load_postgis.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts tests
git commit -m "feat: add optional transactional PostGIS load"
```

### Task 5: Meeting Record Crawl and Guarded Extraction

**Files:**
- Create: `scripts/crawl_review_records.py`
- Create: `scripts/extraction_schema.py`
- Create: `scripts/extract_cases.py`
- Create: `tests/fixtures/meeting_index.html`
- Create: `tests/fixtures/meeting_detail.html`
- Create: `tests/test_crawl_review_records.py`
- Create: `tests/test_extract_cases.py`

**Interfaces:**
- CLI: `python scripts/crawl_review_records.py --out raw/review_meetings/ --kind review|committee`。
- GitHub output: `new_files`。
- CLI: `python scripts/extract_cases.py --in raw/review_meetings/ --out extracted/`。
- Each extracted field is `EvidenceField(value, page, quote_snippet, confidence)`。

- [ ] **Step 1: Write failing crawler tests**

Cover pagination, only meeting-record PDF attachments, committee/review keyword filters, safe filenames, hash de-duplication, immutable downloads, ROC date parsing, and output count.

- [ ] **Step 2: Run crawler RED**

Run: `python -m pytest tests/test_crawl_review_records.py -q`
Expected: FAIL because crawler does not exist.

- [ ] **Step 3: Implement crawler**

Parse official Culture Bureau list/detail pages, limit navigation to official Taipei domains, download PDFs with a size cap, store by Gregorian year-month, and write a source manifest containing page URL, attachment URL, SHA-256, title, and publication date.

- [ ] **Step 4: Run crawler GREEN**

Run: `python -m pytest tests/test_crawl_review_records.py -q`
Expected: PASS.

- [ ] **Step 5: Write failing extraction tests**

Cover valid evidence fields, nulling any value without a valid page and exact snippet, appending failures, `pending` review status, raw model JSON fence cleanup, and rejection of unknown confidence labels.

- [ ] **Step 6: Run extraction RED**

Run: `python -m pytest tests/test_extract_cases.py -q`
Expected: FAIL because extraction modules do not exist.

- [ ] **Step 7: Implement guarded extraction**

Use local PDF text extraction first and OCR-produced text files when present. Request JSON from Anthropic only when `ANTHROPIC_API_KEY` is set; otherwise exit successfully with an explicit pending reason. Validate every evidence field against source page text, emit `extracted/YYYY-MM/<stem>.json` with `review_status: pending`, and write deterministic `extraction_failures.json`.

- [ ] **Step 8: Run extraction GREEN**

Run: `python -m pytest tests/test_extract_cases.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add scripts tests
git commit -m "feat: crawl and safely extract tree-review records"
```

### Task 6: Health, Gap, and Schedule Reports

**Files:**
- Create: `scripts/health_check.py`
- Create: `scripts/gap_report.py`
- Create: `scripts/fetch_schedule.py`
- Create: `tests/test_health_check.py`
- Create: `tests/test_gap_report.py`
- Create: `tests/test_fetch_schedule.py`

**Interfaces:**
- CLI: `python scripts/health_check.py --out reports/health.json`。
- CLI: `python scripts/gap_report.py --health reports/health.json --out reports/gaps.json`。
- CLI: `python scripts/fetch_schedule.py --out raw/pruning_schedules/`。

- [ ] **Step 1: Write failing report tests**

Cover HTTP success/failure, content validation, retention of first `unavailable_since`, recovery clearing unavailable state, explicit `not_configured`, stale snapshot age, missing protected-tree source, missing pruning source, extraction pending/failures, and stable JSON ordering.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_health_check.py tests/test_gap_report.py tests/test_fetch_schedule.py -q`
Expected: FAIL because report modules do not exist.

- [ ] **Step 3: Implement honest status reporting**

Health probes must never log secrets. Gap output must include per-source status, evidence paths, age in days, and a Traditional Chinese public-facing message such as `本資料自 YYYY-MM-DD 起未能更新`. Schedule fetching uses the same immutable storage contract and reports `not_configured` when no official stable URL is configured.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_health_check.py tests/test_gap_report.py tests/test_fetch_schedule.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts tests
git commit -m "feat: publish source health and transparency gap reports"
```

### Task 7: GitHub Actions Workflows and Security Guards

**Files:**
- Create: `.github/workflows/daily-opendata.yml`
- Create: `.github/workflows/weekly-schedule.yml`
- Create: `.github/workflows/monthly-review.yml`
- Create: `.github/workflows/quarterly-committee.yml`
- Create: `.github/workflows/health-check.yml`
- Create: `.github/workflows/gap-report.yml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/dependabot.yml`
- Create: `tests/test_workflows.py`

**Interfaces:**
- Workflows call only the tested CLI interfaces from Tasks 2–6.
- Daily anomaly issue body reads `reports/anomalies.json`.
- Monthly/quarterly extraction opens `needs-human-review` PRs.

- [ ] **Step 1: Write failing workflow contract tests**

Parse YAML and assert exact cron values, `workflow_dispatch`, permissions, timeouts, Python 3.12, pinned major action versions, daily `concurrency.group: data-sync`, `cancel-in-progress: false`, secrets/vars locations, anomaly issue condition, extraction PR rather than direct push, fork-safe secret condition, and all six required filenames.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_workflows.py -q`
Expected: FAIL because workflows do not exist.

- [ ] **Step 3: Implement workflows**

Use `actions/checkout@v4`, `actions/setup-python@v5`, `actions/github-script@v7`, and `peter-evans/create-pull-request@v6`. Commit only declared data/report paths. Use retry-safe pull/rebase before push, artifact upload for failure diagnostics, issue de-duplication by title/label, and optional webhook notification without exposing its value.

- [ ] **Step 4: Run GREEN**

Run: `python -m pytest tests/test_workflows.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github tests
git commit -m "ci: automate Taipei tree transparency pipelines"
```

### Task 8: End-to-End Verification and Operations Documentation

**Files:**
- Create: `docs/operations.md`
- Create: `docs/data-contract.md`
- Create: `tests/test_cli_smoke.py`
- Modify: `README.md`

**Interfaces:**
- A fixture-based offline smoke run produces raw, processed, extracted-pending, anomalies, health, and gaps artifacts without network or secrets.

- [ ] **Step 1: Write failing offline smoke test**

The test invokes CLIs through their `main(argv)` functions in a temporary repository and asserts all expected artifacts, schema versions, immutable behavior, and deterministic second run.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_cli_smoke.py -q`
Expected: FAIL until all CLI composition hooks exist.

- [ ] **Step 3: Complete integration and documentation**

Document repository variables/secrets, first manual run order, branch protection, reviewer setup, Git LFS/R2 threshold, recovery from failed pushes, 60-day schedule caveat, data retention, anomaly triage, extraction review checklist, source URL updates, and how `/watch/gaps` consumers read `health.json`/`gaps.json`.

- [ ] **Step 4: Run full verification**

Run:

```bash
python -m pytest -q
python -m compileall -q scripts tests
python -m ruff check scripts tests
python -m ruff format --check scripts tests
```

Expected: all commands exit 0 with zero failures/errors.

- [ ] **Step 5: Validate workflow YAML**

Run: `python -m pytest tests/test_workflows.py -q`
Expected: PASS for all workflow contracts.

- [ ] **Step 6: Commit**

```bash
git add README.md docs tests scripts
git commit -m "docs: complete pipeline operations and verification"
```

