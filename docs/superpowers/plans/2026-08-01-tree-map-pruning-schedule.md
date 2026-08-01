# Tree Map and Pruning Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official park trees, Google Maps locations, official street/park pruning schedules, and evidence-labelled candidate-tree matching to the deployed mobile-friendly GitHub Pages site.

**Architecture:** Extend the existing immutable snapshot pipeline with park-tree input and a dedicated official schedule parser. Convert coordinates and compute conservative schedule matches at build time, then publish only validated static JSON consumed by a progressively enhanced frontend.

**Tech Stack:** Python 3.12, pandas, pyarrow, pyproj, BeautifulSoup, httpx, pytest, vanilla ES modules/CSS, Node test runner, GitHub Actions, GitHub Pages.

## Global Constraints

- Candidate trees must always be labelled `可能受影響樹木`; never claim they are an official per-tree work list.
- Never infer a village chief, councillor, or reporting contractor name from geography, office, or work unit.
- Invalid or missing coordinates produce no map URL; they must not fall back to address search.
- Official source failure must not overwrite the last valid generated site data.
- All official-source text must be rendered with `textContent`, and all external URLs must pass an allow-list.
- The public site remains keyboard accessible and mobile friendly.
- No paid map SDK or API key.

---

### Task 1: Add park-tree ingestion and canonical fields

**Files:**
- Modify: `config/sources.json`
- Modify: `scripts/normalize.py`
- Modify: `scripts/fetch_opendata.py`
- Modify: `tests/fixtures/street_trees.csv`
- Create: `tests/fixtures/park_trees.csv`
- Modify: `tests/test_config.py`
- Modify: `tests/test_normalize.py`
- Modify: `tests/test_fetch_opendata.py`

**Interfaces:**
- Consumes: existing `SourceConfig`, immutable CSV archive, `normalize_all(raw_dir, out_dir)`.
- Produces: canonical `tree_type` and `park_name`; `processed/park_trees.parquet`; configured source `park_trees` at `https://tppkl.blob.core.windows.net/blobfs/TaipeiParkTree.csv`.

- [ ] **Step 1: Write failing tests**

Add fixture rows with `TreeID,Dist,ParkName,TreeType,Diameter,TreeHeight,TWD97X,TWD97Y,SurveyDate,UpdDate`. Assert `normalize_rows(..., "park_trees", ...)` maps `ParkName` to `park_name`, sets `tree_type == "park"`, maps `UpdDate`, and that `normalize_all` writes `park_trees.parquet`. Assert configuration requires both public tree sources.

```python
assert frame.loc[0, "park_name"] == "大安森林公園"
assert frame.loc[0, "tree_type"] == "park"
assert (out_dir / "park_trees.parquet").exists()
assert sources["park_trees"].url == "https://tppkl.blob.core.windows.net/blobfs/TaipeiParkTree.csv"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_config.py tests/test_normalize.py tests/test_fetch_opendata.py -q`

Expected: failures for missing `park_trees`, `park_name`, and `tree_type`.

- [ ] **Step 3: Implement canonical park-tree support**

Add `tree_type` and `park_name` to `CANONICAL_COLUMNS`; add `ParkName` alias. Set `tree_type` from source (`street` or `park`) after row mapping. Add the official park CSV source and write the latest park snapshot to `park_trees.parquet`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_config.py tests/test_normalize.py tests/test_fetch_opendata.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Run: `git add config/sources.json scripts/normalize.py scripts/fetch_opendata.py tests && git commit -m "feat: ingest official park trees"`

### Task 2: Convert and validate tree coordinates

**Files:**
- Create: `scripts/coordinates.py`
- Create: `tests/test_coordinates.py`
- Modify: `scripts/requirements.txt`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `twd97_to_wgs84(x: object, y: object) -> tuple[float, float] | None` returning `(latitude, longitude)` rounded to 7 decimals.

- [ ] **Step 1: Write failing coordinate tests**

```python
def test_twd97_to_wgs84_known_taipei_point():
    latitude, longitude = twd97_to_wgs84(306894.85, 2770248.38)
    assert latitude == pytest.approx(25.033964, abs=0.00002)
    assert longitude == pytest.approx(121.564468, abs=0.00002)

@pytest.mark.parametrize("x,y", [(None, 2770248), (float("nan"), 2770248), (0, 0)])
def test_invalid_coordinates_return_none(x, y):
    assert twd97_to_wgs84(x, y) is None
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/test_coordinates.py -q`

Expected: import failure for `scripts.coordinates`.

- [ ] **Step 3: Implement conversion**

Use one module-level `pyproj.Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True)`. Reject non-finite TWD97 values and converted values outside latitude `24.8..25.3` or longitude `121.3..121.8`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_coordinates.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run: `git add pyproject.toml scripts/requirements.txt scripts/coordinates.py tests/test_coordinates.py && git commit -m "feat: convert tree coordinates for maps"`

### Task 3: Parse official pruning schedules

**Files:**
- Create: `scripts/parse_pruning_schedule.py`
- Create: `tests/fixtures/pruning_index.html`
- Create: `tests/fixtures/pruning_street.html`
- Create: `tests/fixtures/pruning_park.html`
- Create: `tests/test_parse_pruning_schedule.py`
- Modify: `scripts/fetch_schedule.py`
- Modify: `config/sources.json`

**Interfaces:**
- Produces: `discover_schedule_urls(html: bytes, base_url: str) -> dict[str, str]`.
- Produces: `parse_schedule(html: bytes, category: str, source_url: str, retrieved_at: datetime) -> list[dict[str, object]]`.
- Produces: `build_schedule_document(...) -> {"schema_version": 1, "schedules": [...]}`.

- [ ] **Step 1: Write failing parser tests**

Fixtures must represent the official headers. Assert the index discovers the latest street and park content links. Assert a street row produces dates, location, count, work unit, basis, `requester_type`, and `requester_name is None`; assert a park row produces district, park name and responsible unit.

```python
assert item["locations"] == ["民生東路四段"]
assert item["planned_count"] == 152
assert item["work_unit"] == "海棠園藝有限公司"
assert item["basis"] == "里長建議"
assert item["requester_type"] == "village_chief_recommendation"
assert item["requester_name"] is None
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/test_parse_pruning_schedule.py -q`

Expected: import failure for the new parser.

- [ ] **Step 3: Implement strict parsing and archive integration**

Parse tables by normalized header names, convert ROC years by adding 1911, construct deterministic SHA-256 schedule IDs, and reject missing required headers. Extend `fetch_schedule.py` to archive index/detail HTML plus a normalized `latest.json` atomically. Configure the official index URL.

- [ ] **Step 4: Run parser and existing fetch tests**

Run: `python -m pytest tests/test_parse_pruning_schedule.py tests/test_fetch_schedule.py -q`

Expected: all tests pass, including redirect, allow-list, bounded download and immutable archive cases.

- [ ] **Step 5: Commit**

Run: `git add config/sources.json scripts/parse_pruning_schedule.py scripts/fetch_schedule.py tests && git commit -m "feat: parse official pruning schedules"`

### Task 4: Match schedules conservatively

**Files:**
- Create: `scripts/match_pruning.py`
- Create: `tests/test_match_pruning.py`

**Interfaces:**
- Produces: `normalize_place(value: object) -> str`.
- Produces: `match_schedules(trees: list[dict], schedules: list[dict]) -> list[dict]` with `schedule_id`, `tree_id`, `match_method`, `explanation`.

- [ ] **Step 1: Write failing matching tests**

Test exact normalized road phrase matching, park district+name matching, rejection of short/ambiguous phrases, district mismatch, and no inference of requester identity.

```python
assert matches == [{
    "schedule_id": "schedule-1",
    "tree_id": "tree-1",
    "match_method": "street_location_phrase",
    "explanation": "依完整路段名稱比對，並非官方逐株施工名單",
}]
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/test_match_pruning.py -q`

Expected: import failure for the matcher.

- [ ] **Step 3: Implement deterministic matching**

Normalize Unicode NFKC, whitespace, punctuation and `台` to `臺`. Require at least two CJK/alphanumeric characters after removing directional-only suffixes. For park schedules require both district and full park name. Sort results by `(schedule_id, tree_id)` and never truncate to `planned_count`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_match_pruning.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run: `git add scripts/match_pruning.py tests/test_match_pruning.py && git commit -m "feat: match possible pruning trees conservatively"`

### Task 5: Build and validate the public data contract

**Files:**
- Modify: `scripts/build_site_data.py`
- Modify: `scripts/validate_site_data.py`
- Modify: `tests/test_build_site_data.py`
- Modify: `tests/test_validate_site_data.py`
- Modify: `docs/data-contract.md`

**Interfaces:**
- `build_site_data(street_parquet, output_dir, park_parquet=None, schedule_path=None)` writes manifest schema v2, district JSON, `schedules.json`, and `schedule_matches.json`.
- Public tree records add `tree_type`, `park_name`, `latitude`, `longitude`, and `schedule_ids`.

- [ ] **Step 1: Write failing contract tests**

Assert combined counts, map coordinates, source-specific freshness, schedule data, match references, deterministic output, and atomic preservation after a deliberately invalid schedule document.

```python
assert manifest["schema_version"] == 2
assert manifest["type_counts"] == {"park": 1, "street": 2}
assert tree["latitude"] == pytest.approx(25.033964, abs=0.00002)
assert tree["schedule_ids"] == ["schedule-1"]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_build_site_data.py tests/test_validate_site_data.py -q`

Expected: failures for the old function signature and schema v1.

- [ ] **Step 3: Implement schema v2 atomically**

Load optional park and schedule inputs, transform coordinates, generate matches, and write all artifacts in the existing staging directory before replacement. Validate unique tree IDs using a source-qualified public ID (`street:<id>` or `park:<id>`), coordinate pairs, schedule fields, match references, allowed government source hosts, and aggregate counts.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/test_build_site_data.py tests/test_validate_site_data.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run: `git add scripts/build_site_data.py scripts/validate_site_data.py tests/test_build_site_data.py tests/test_validate_site_data.py docs/data-contract.md && git commit -m "feat: publish maps and pruning data"`

### Task 6: Add the mobile UI and accessible interactions

**Files:**
- Modify: `site/index.html`
- Modify: `site/styles.css`
- Modify: `site/search.mjs`
- Modify: `site/app.js`
- Modify: `tests/site-search.test.mjs`
- Modify: `tests/test_site_contract.py`

**Interfaces:**
- `filterTrees` accepts `treeType` in addition to district/location/species.
- `buildGoogleMapsUrl(tree) -> string | null` only returns URLs for valid numeric coordinate pairs.
- The page includes `#tree-type`, `#schedule-list`, `#schedule-type`, and `#schedule-query`.

- [ ] **Step 1: Write failing JavaScript and HTML contract tests**

```javascript
assert.equal(buildGoogleMapsUrl({ latitude: 25.033964, longitude: 121.564468 }),
  "https://www.google.com/maps/search/?api=1&query=25.033964%2C121.564468");
assert.equal(buildGoogleMapsUrl({ latitude: null, longitude: null }), null);
assert.deepEqual(filterTrees(records, { treeType: "park", district: "", location: "", species: "" }), [records[1]]);
```

Assert required headings, source disclaimer, schedule filters, map action, footer links, viewport, skip link and accessible labels.

- [ ] **Step 2: Run tests and verify RED**

Run: `node --test tests/site-search.test.mjs && python -m pytest tests/test_site_contract.py -q`

Expected: failures for absent exports and DOM elements.

- [ ] **Step 3: Implement the UI**

Add tree-type filtering, map links, schedule badges, expandable details, and a dedicated schedule section. Use DOM creation plus `textContent`; never interpolate official data into `innerHTML`. Render desktop rows and mobile cards through responsive CSS, keep 44px minimum touch targets, and retain footer links.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `node --test tests/site-search.test.mjs && python -m pytest tests/test_site_contract.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run: `git add site tests/site-search.test.mjs tests/test_site_contract.py && git commit -m "feat: show tree maps and pruning schedules"`

### Task 7: Wire automation, verify deployment, push and open PR

**Files:**
- Modify: `.github/workflows/daily-opendata.yml`
- Modify: `.github/workflows/weekly-schedule.yml`
- Modify: `.github/workflows/pages.yml`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_workflow_cli_existence.py`
- Modify: `README.md`
- Modify: `docs/operations.md`

**Interfaces:**
- Daily data workflow fetches both tree sources and rebuilds public data.
- Schedule workflow runs daily, archives/parses schedules, rebuilds public data, validates output, and commits only changed artifacts.
- Pages workflow validates schema v2 before upload.

- [ ] **Step 1: Write failing workflow tests**

Assert park source fetch, schedule parser/build invocation, daily cron, schema validation before deploy, concurrency controls, least-privilege permissions, and referenced CLI file existence.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_workflows.py tests/test_workflow_cli_existence.py -q`

Expected: failures for missing park/schedule build commands and weekly cron.

- [ ] **Step 3: Update workflows and documentation**

Wire the exact CLI paths from Tasks 1–5, preserve last valid derived output on failures, document schedule evidence semantics, local build commands, update cadence, GitHub Pages URL, and limitations.

- [ ] **Step 4: Run full local verification**

Run: `python -m pytest -q`

Expected: all Python tests pass.

Run: `node --test tests/site-search.test.mjs`

Expected: all JavaScript tests pass.

Run: `python -m ruff check .`

Expected: no lint errors.

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intentional files changed before commit.

- [ ] **Step 5: Commit, push and create PR**

Run: `git add .github README.md docs/operations.md tests && git commit -m "ci: automate tree and pruning updates"`

Run: `git push -u origin feat/github-actions-data-pipeline`

Create or update the pull request into `main`, include requirement summary and verification evidence, wait for GitHub Actions checks, and verify the public repository Pages deployment. Do not report completion until the deployed site loads manifest schema v2 with non-zero tree data and the schedule section presents either current official records or a transparent source-status message.
