# Protected Trees, Species Profiles, and Village Leaders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official Taipei protected trees, official age/photo/story details, data-driven species profiles, and verified village-leader contact cards for village-chief pruning recommendations to the mobile-friendly GitHub Pages site.

**Architecture:** Keep GitHub Pages fully static. GitHub Actions fetches the official protected-tree CSV, incrementally enriches it through the official detail API, generates compact protected-tree/species/village-leader JSON, and validates the complete artifact before deployment. Existing street and park data remain canonical; protected-only fields live in a focused enrichment document joined by public ids such as `protected:668` during site-data generation.

**Tech Stack:** Python 3.12, pandas, pyarrow, httpx, BeautifulSoup, pytest, JavaScript ES modules, Node test runner, static HTML/CSS, GitHub Actions, GitHub Pages.

## Global Constraints

- Only official Taipei data may supply age, planting year, photos, stories, village, village leader, and public phone values.
- Never infer ordinary street/park-tree age or copy protected-tree attributes onto another tree.
- Missing official values render as `官方未提供`; a not-yet-fetched detail renders as `詳細資料同步中`.
- Only `https:` official URLs are emitted; phone links contain only digits, `+`, and `-`.
- Photos stay on official servers; the repository stores URLs and metadata only.
- Protected detail API calls use low concurrency, retry limits, request spacing, and incremental cache preservation.
- Ambiguous/cross-village locations remain unresolved or cross-village; no fuzzy assignment.
- The site remains usable on a 320 px viewport without horizontal page scrolling.

---

### Task 1: Normalize Official Protected-Tree CSV

**Files:**
- Modify: `config/sources.json`
- Modify: `scripts/normalize.py`
- Modify: `tests/test_normalize.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_fetch_opendata.py`
- Create: `tests/fixtures/protected_trees.csv`

**Interfaces:**
- Consumes: existing `fetch_dataset()` and official resource id `a7c2db0d-8b6e-42b2-bdcc-ae69a6797e1d`.
- Produces: `processed/protected_trees.parquet` with the extended `CANONICAL_COLUMNS` schema and `tree_type == "protected"`.

- [ ] **Step 1: Add a protected-tree CSV fixture and failing normalization tests**

```python
def test_normalize_protected_tree_official_columns():
    frame, _ = normalize_rows(PROTECTED_FIXTURE.read_bytes(), "protected_trees", date(2026, 8, 2))
    row = frame.iloc[0]
    assert row["tree_type"] == "protected"
    assert row["tree_id"] == "668"
    assert row["species"] == "榕"
    assert row["scientific_name"] == "Ficus microcarpa L. f."
    assert row["english_name"] == "Banyan"
    assert row["latitude"] == pytest.approx(25.033478)
    assert row["longitude"] == pytest.approx(121.547514)
    assert row["management_unit"] == "財團法人台灣郵政協會"
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_normalize.py tests/test_config.py tests/test_fetch_opendata.py -q`

Expected: failures because protected aliases/columns and required source configuration do not exist.

- [ ] **Step 3: Extend canonical aliases and protected normalization**

Add exact canonical fields:

```python
"scientific_name", "english_name", "management_unit", "latitude", "longitude"
```

Add official aliases:

```python
"tree_id": (..., "樹木編號"),
"species": (..., "樹種名稱"),
"scientific_name": ("樹種學名",),
"english_name": ("英文名",),
"diameter_cm": (..., "樹胸徑寬度公尺"),
"location": (..., "地址"),
"management_unit": ("管理單位",),
"latitude": ("緯度",),
"longitude": ("經度",),
```

For `protected_trees`, set `tree_type = "protected"` and convert official diameter metres to centimetres without changing street/park values.

- [ ] **Step 4: Configure the official dataset and resource URL**

```json
"protected_trees": {
  "dataset_id": "d9d1140b-c2c3-405f-8dd1-7b87b00f6f53",
  "url": "https://data.taipei/api/frontstage/tpeod/dataset/resource.download?rid=a7c2db0d-8b6e-42b2-bdcc-ae69a6797e1d",
  "required": true
}
```

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_normalize.py tests/test_config.py tests/test_fetch_opendata.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add config/sources.json scripts/normalize.py tests/test_normalize.py tests/test_config.py tests/test_fetch_opendata.py tests/fixtures/protected_trees.csv
git commit -m "feat: ingest official protected trees"
```

---

### Task 2: Build Incremental Protected-Tree Detail Cache

**Files:**
- Create: `scripts/fetch_protected_details.py`
- Create: `tests/test_fetch_protected_details.py`
- Modify: `scripts/requirements.txt` only if an existing dependency is missing

**Interfaces:**
- Consumes: protected codes from `processed/protected_trees.parquet`, optional prior JSON cache, `httpx.Client`.
- Produces: `processed/protected_tree_details.json` schema version 1.
- Exposes: `compact_detail(payload: dict, fetched_at: datetime) -> dict`, `choose_codes(codes, cache, limit) -> list[str]`, and `refresh_details(...) -> dict`.

- [ ] **Step 1: Write failing tests for compact official fields**

```python
def test_compact_detail_keeps_official_age_photo_story_and_source():
    result = compact_detail(API_PAYLOAD_668, FIXED_TIME)
    assert result["code"] == "668"
    assert result["age_years"] == 55
    assert result["born_year"] == 1971
    assert result["photo_count"] == 2
    assert result["photo_url"].startswith("https://ecultureuser.gov.taipei/")
    assert result["official_detail_url"].endswith("/tree/668")
    assert result["detail_status"] == "available"
```

Add tests for no images, no age/story, unsafe photo URL rejection, API failure preserving old cache, new/missing records first, and oldest fetched records next.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python -m pytest tests/test_fetch_protected_details.py -q`

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement safe compaction and deterministic rotation**

The output record contains only:

```python
{
    "code": code,
    "village": clean_text(payload.get("villageName")),
    "age_years": clean_int(payload.get("age")),
    "born_year": clean_int(payload.get("bornYear")),
    "photo_url": first_official_https_image(payload.get("images", [])),
    "photo_count": len(valid_official_images),
    "story": clean_text(payload.get("historyInfo")),
    "environment_description": clean_text(payload.get("envDescription")),
    "official_modified_at": clean_iso(payload.get("modifyDate")),
    "official_detail_url": f"https://eculture.gov.taipei/trees/zh-tw/tree/{quote(code)}",
    "detail_status": "available",
    "detail_fetched_at": fetched_at.isoformat(),
}
```

Use a default limit of 300, one worker, 150 ms spacing, three attempts, and exponential delays of 0.5/1.0 seconds. Preserve old records on every per-code failure and include a compact `errors` list.

- [ ] **Step 4: Add CLI and atomic write**

```text
python scripts/fetch_protected_details.py \
  --src processed/protected_trees.parquet \
  --previous-url https://u8901006.github.io/taipei-trees-data/data/protected_tree_details.json \
  --out processed/protected_tree_details.json \
  --limit 300
```

Support `--limit 0` for an explicit, rate-limited first bootstrap of all missing records.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_fetch_protected_details.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/fetch_protected_details.py tests/test_fetch_protected_details.py
git commit -m "feat: cache protected tree details"
```

---

### Task 3: Resolve Villages and Parse Official Village-Leader Profiles

**Files:**
- Create: `scripts/enrich_village_leaders.py`
- Create: `config/park_villages.json`
- Create: `tests/test_enrich_village_leaders.py`
- Create: `tests/fixtures/village_leader_dongrong.html`
- Modify: `scripts/parse_pruning_schedule.py`
- Modify: `tests/test_parse_pruning_schedule.py`

**Interfaces:**
- Consumes: `processed/pruning_schedule.json`, official profile pages, and source-backed park-village crosswalk.
- Produces: `processed/pruning_schedule_enriched.json` with village match evidence and leader fields.
- Exposes: `parse_leader_profile(html, url) -> VillageLeader`, `resolve_schedule_village(schedule, crosswalk) -> VillageMatch`, and `enrich_schedules(...) -> dict`.

- [ ] **Step 1: Write failing parser and resolver tests**

```python
def test_parse_dongrong_leader_profile():
    profile = parse_leader_profile(FIXTURE.read_text(encoding="utf-8"), OFFICIAL_URL)
    assert profile.district == "松山區"
    assert profile.village == "東榮里"
    assert profile.name == "鄭玉梅"
    assert profile.mobile == "0933902948"
    assert profile.profile_url == OFFICIAL_URL

def test_fujin_park_crosswalk_requires_source_evidence():
    match = resolve_schedule_village(FUJIN_SCHEDULE, CROSSWALK)
    assert match.village == "東榮里"
    assert match.method == "park_crosswalk"
    assert match.source_url.startswith("https://")
```

Add tests for cross-village, unresolved location, non-village-chief schedules, and no fuzzy match for `東隆里`.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python -m pytest tests/test_enrich_village_leaders.py tests/test_parse_pruning_schedule.py -q`

Expected: missing module/fields.

- [ ] **Step 3: Implement strict official-page parsing**

Use BeautifulSoup and anchored labels (`里長簡介`, `里長行動電話`, breadcrumb district/village). Normalize display phone separately from raw digits. Reject profiles if district, village, name, mobile, or official HTTPS URL validation fails.

- [ ] **Step 4: Implement evidence-backed crosswalk enrichment**

Each `config/park_villages.json` entry contains:

```json
{
  "park_name": "富錦一號公園",
  "district": "松山區",
  "villages": ["東榮里"],
  "match_method": "manual_verified",
  "source_url": "https://ssdo.gov.taipei/News.aspx?n=168EE47B876839FB&sms=3F9632016583341A",
  "verified_at": "2026-08-02"
}
```

Do not add this mapping until a formal official geographic source confirms it. If no confirmation is available, keep the schedule `unresolved` while still shipping the verified East Rong Village leader fixture/parser.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_enrich_village_leaders.py tests/test_parse_pruning_schedule.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/enrich_village_leaders.py config/park_villages.json scripts/parse_pruning_schedule.py tests/test_enrich_village_leaders.py tests/test_parse_pruning_schedule.py tests/fixtures/village_leader_dongrong.html
git commit -m "feat: add verified village leader metadata"
```

---

### Task 4: Generate Protected Records and Species Profiles

**Files:**
- Modify: `scripts/build_site_data.py`
- Modify: `scripts/validate_site_data.py`
- Modify: `tests/test_build_site_data.py`
- Modify: `tests/test_validate_site_data.py`

**Interfaces:**
- Consumes: street/park/protected parquet, detail cache, enriched schedules.
- Produces: district partitions including protected records, `species_profiles.json`, detail coverage metrics, schema-version 3 manifest.

- [ ] **Step 1: Write failing site-data tests**

```python
def test_build_includes_protected_tree_and_species_profile(tmp_path):
    manifest = build_site_data(..., protected_parquet_path=protected, protected_details_path=details)
    assert manifest["type_counts"]["protected"] == 1
    assert manifest["protected_detail_coverage"]["available"] == 1
    profiles = read_json(tmp_path / "data/species_profiles.json")
    assert profiles["榕"]["tree_count"] == 3
    assert profiles["榕"]["scientific_name"] == "Ficus microcarpa L. f."
```

Add assertions for protected `latitude/longitude`, `age_source`, direct photo/detail URLs, missing status, averages ignoring nulls, district counts, common locations, and conflicting official scientific names.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python -m pytest tests/test_build_site_data.py tests/test_validate_site_data.py -q`

Expected: new parameters/schema fields unsupported.

- [ ] **Step 3: Extend frame preparation and public record generation**

Preserve protected WGS84 coordinates instead of passing them through TWD97 conversion. Join detail cache by `tree_id`, emit `age_source = "official_protected_tree_registry"` only when age/born year exists, and emit a `detail_status` value for every protected record.

- [ ] **Step 4: Build deterministic species profiles**

Group by normalized species name and emit counts, district counts, top five nonblank locations, means rounded to one decimal, protected-tree count, official names with conflict metadata, and encoded HTTPS search links for TAI2/TBN.

- [ ] **Step 5: Strengthen deployment validation**

Require schema version 3, exact tree type keys `street`, `park`, `protected`, safe protected/detail/photo URLs, profile count consistency, coverage totals, schedule-village field types, and no leader contact fields on non-village-chief schedules.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `python -m pytest tests/test_build_site_data.py tests/test_validate_site_data.py -q`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add scripts/build_site_data.py scripts/validate_site_data.py tests/test_build_site_data.py tests/test_validate_site_data.py
git commit -m "feat: publish protected trees and species profiles"
```

---

### Task 5: Add Mobile Protected-Tree, Species, and Leader UI

**Files:**
- Modify: `site/index.html`
- Modify: `site/app.js`
- Modify: `site/search.mjs`
- Modify: `site/styles.css`
- Modify: `tests/site-search.test.mjs`
- Modify: `tests/test_site_contract.py`

**Interfaces:**
- Consumes: manifest schema 3, district records, `species_profiles.json`, enriched schedules.
- Produces: accessible protected-tree detail cards/dialogs, species profile panel, and village-leader schedule card.

- [ ] **Step 1: Write failing JavaScript and HTML contract tests**

```javascript
test("protected details distinguish missing from pending", () => {
  assert.equal(protectedValue({detail_status: "pending"}, "age_years"), "詳細資料同步中");
  assert.equal(protectedValue({detail_status: "available", age_years: null}, "age_years"), "官方未提供");
});

test("official links reject non-HTTPS and non-Taipei hosts", () => {
  assert.equal(validateOfficialUrl("javascript:alert(1)"), null);
});
```

HTML tests require a protected option, protected/species detail container, data-status metrics, and unchanged clinic/newsletter/coffee footer links.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `node --test tests/site-search.test.mjs && python -m pytest tests/test_site_contract.py -q`

Expected: missing exports/elements.

- [ ] **Step 3: Add protected and species presentation helpers**

Export pure helpers from `search.mjs` for official URL validation, protected missing/pending copy, phone sanitization, and species profile lookup. In `app.js`, build all untrusted content with `textContent`; never assign external story HTML.

- [ ] **Step 4: Update result and schedule cards**

Add protected badge, official age, village, representative photo link, story/environment summary, Maps/detail links, clickable species name, and the village-leader card only for `village_chief_recommendation` with verified data.

- [ ] **Step 5: Add responsive styles**

At widths below 760 px, render tree results as cards, make detail/actions stack vertically, constrain images, allow long URLs/text to wrap, and verify `overflow-x` is not introduced at 320 px.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run: `node --test tests/site-search.test.mjs && python -m pytest tests/test_site_contract.py -q`

Expected: all selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add site/index.html site/app.js site/search.mjs site/styles.css tests/site-search.test.mjs tests/test_site_contract.py
git commit -m "feat: show protected trees species and village leaders"
```

---

### Task 6: Wire GitHub Actions, Documentation, and End-to-End Verification

**Files:**
- Modify: `.github/workflows/pages.yml`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_workflow_cli_existence.py`
- Modify: `README.md`
- Modify: `docs/data-contract.md`
- Modify: `docs/operations.md`

**Interfaces:**
- Consumes: all scripts from Tasks 1–5.
- Produces: deployable `_site` artifact and documented operational recovery path.

- [ ] **Step 1: Write failing workflow contract tests**

Require workflow commands for protected details, village enrichment, protected/site arguments, schema validation, and every changed path trigger.

- [ ] **Step 2: Run workflow tests and confirm RED**

Run: `python -m pytest tests/test_workflows.py tests/test_workflow_cli_existence.py -q`

Expected: commands and path triggers missing.

- [ ] **Step 3: Update the Pages workflow**

Run commands in this order:

```yaml
- python scripts/fetch_opendata.py --out raw/open_data/ --date "${{ steps.taipei-date.outputs.value }}"
- python scripts/normalize.py --raw raw/open_data/ --out processed/
- python scripts/fetch_schedule.py --out raw/pruning_schedules/ --processed-out processed/pruning_schedule.json
- python scripts/enrich_village_leaders.py --schedule processed/pruning_schedule.json --crosswalk config/park_villages.json --out processed/pruning_schedule_enriched.json
- python scripts/fetch_protected_details.py --src processed/protected_trees.parquet --previous-url "${{ vars.PROTECTED_DETAILS_PREVIOUS_URL }}" --out processed/protected_tree_details.json --limit 300
- python scripts/build_site_data.py --src processed/trees.parquet --park-src processed/park_trees.parquet --protected-src processed/protected_trees.parquet --protected-details processed/protected_tree_details.json --schedule processed/pruning_schedule_enriched.json --out _site/data
- python scripts/validate_site_data.py --data _site/data --minimum-total 50000 --minimum-protected 3000 --expected-districts 12
```

Default the previous URL in the script when the repository variable is absent.

- [ ] **Step 4: Document sources, semantics, and recovery**

Document official source URLs, `官方未提供` versus `詳細資料同步中`, detail-cache rotation, village evidence methods, phone/public-data policy, manual full bootstrap, validation thresholds, and rollback behavior.

- [ ] **Step 5: Run the complete local verification suite**

Run:

```text
python -m pytest -q
node --test tests/site-search.test.mjs
python -m ruff check scripts tests
```

Expected: zero failures and zero lint errors.

- [ ] **Step 6: Build a deterministic fixture-backed site and validate it**

Run the deterministic integration test that builds fixture-backed `_site/data` and invokes the validator:

```text
python -m pytest tests/test_build_site_data.py::test_generated_fixture_passes_site_validation -q
```

Expected: exit 0 and every referenced JSON file exists.

- [ ] **Step 7: Browser acceptance test**

Serve `_site`, open it in the in-app browser, and verify at desktop and 320 px viewport:

- protected filtering and record detail;
- Google Maps and official detail/photo links;
- species profile contents;
- missing versus pending text;
- village-chief card and official profile link;
- no horizontal page overflow;
- existing street/park search and pruning filters still work.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/pages.yml tests/test_workflows.py tests/test_workflow_cli_existence.py README.md docs/data-contract.md docs/operations.md
git commit -m "ci: deploy protected tree transparency features"
```

---

### Task 7: Publish Branch and Pull Request

**Files:**
- No production files; VCS and GitHub state only.

**Interfaces:**
- Consumes: verified commits from Tasks 1–6.
- Produces: pushed feature branch, open pull request, green GitHub checks, and deployable merge result.

- [ ] **Step 1: Re-run fresh pre-push verification**

Run:

```text
python -m pytest -q
node --test tests/site-search.test.mjs
python -m ruff check scripts tests
git diff --check origin/main...HEAD
git status --short
```

Expected: all tests/lint/diff checks pass and the worktree is clean.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin feat/protected-trees-species-village
```

- [ ] **Step 3: Create a non-draft pull request**

Title: `feat: add protected trees, species profiles, and village leaders`

Body includes source provenance, missing-data semantics, village evidence rule, screenshots/acceptance notes, tests run, and deployment considerations.

- [ ] **Step 4: Monitor checks and repair failures**

Use `gh pr checks --watch` or the GitHub connector. For any failure, inspect the exact log, add a failing regression test, implement the minimum fix, repeat full verification, commit, push, and watch again.

- [ ] **Step 5: Confirm final PR evidence**

Confirm the PR is open against `main`, branch is current, every required check is successful, and the public repository remains `PUBLIC`.
