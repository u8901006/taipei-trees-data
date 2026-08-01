# Public Tree Search Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立並部署一個供一般市民查詢臺北市真實行道樹資料的手機優先 GitHub Pages 網站。

**Architecture:** Python 將 canonical Parquet 轉成依行政區分割的精簡 JSON；原生 HTML/CSS/ES modules 提供無後端搜尋與分頁。GitHub Actions 定期重建資料並部署 Pages，首次發布使用同一份靜態成品建立 `gh-pages`。

**Tech Stack:** Python 3.12、pandas/pyarrow、HTML5、CSS3、ES modules、Node built-in test runner、GitHub Pages Actions。

## Global Constraints

- 搜尋欄位固定為行政區、路段與樹種。
- 結果固定顯示行政區、路段、樹種、胸徑、樹高與更新日期。
- 手機以卡片、桌面以表格呈現；每頁 30 筆。
- 資料只能來自現有官方 Open Data 管線，不得使用假資料代替已部署內容。
- 頁尾必須包含 `https://www.leepsyclinic.com/`、`https://blog.leepsyclinic.com/`、`https://buymeacoffee.com/CYlee`。
- 所有資料文字以安全 DOM API 顯示，不得拼接為 HTML。

---

### Task 1: 建立可重現的網站資料索引

**Files:**
- Create: `scripts/build_site_data.py`
- Create: `tests/test_build_site_data.py`

**Interfaces:**
- Consumes: canonical `processed/trees.parquet`。
- Produces: `build_site_data(parquet_path: Path, output_dir: Path) -> dict[str, object]`、`manifest.json` 與 `districts/<sha256-prefix>.json`。

- [ ] **Step 1: Write the failing tests**

測試兩個行政區輸入，斷言 manifest 的 `total_count`、`latest_update`、排序後 districts，以及每筆只有 `id,district,location,species,diameter,height,updated`；再以兩個輸出目錄斷言 bytes 完全相同。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_build_site_data.py -q`
Expected: FAIL，因 `scripts.build_site_data` 尚不存在。

- [ ] **Step 3: Write minimal implementation**

實作 canonical schema 驗證、null 正規化、行政區排序、SHA-256 檔名、minified UTF-8 JSON 與原子目錄輸出；空資料產生 `total_count: 0` 與空 districts。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_build_site_data.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

`git add scripts/build_site_data.py tests/test_build_site_data.py && git commit -m "feat: build public tree search index"`

### Task 2: 建立搜尋核心與契約測試

**Files:**
- Create: `site/search.mjs`
- Create: `tests/site-search.test.mjs`

**Interfaces:**
- Produces: `normalizeQuery(value)`、`filterTrees(records, filters)`、`paginate(records, page, pageSize)`、`formatMeasurement(value, unit)`。

- [ ] **Step 1: Write the failing Node tests**

覆蓋 NFKC、空白、大小寫、行政區 exact match、路段／樹種 substring、null 測量值與分頁邊界。

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/site-search.test.mjs`
Expected: FAIL，因 `site/search.mjs` 尚不存在。

- [ ] **Step 3: Implement the pure search functions**

使用無 DOM 相依的純函式；`paginate` 回傳 `{items,total,page,pageCount}`，page 固定夾在有效範圍。

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test tests/site-search.test.mjs`
Expected: PASS。

- [ ] **Step 5: Commit**

`git add site/search.mjs tests/site-search.test.mjs && git commit -m "feat: add browser tree search core"`

### Task 3: 建立手機優先查詢頁

**Files:**
- Create: `site/index.html`
- Create: `site/styles.css`
- Create: `site/app.js`
- Create: `tests/test_site_contract.py`

**Interfaces:**
- Consumes: `site/search.mjs`、`data/manifest.json` 與 district JSON。
- Produces: 可鍵盤操作、具 loading/error/empty/results 狀態的單頁網站。

- [ ] **Step 1: Write the failing HTML contract tests**

斷言 title、description、`main`、搜尋表單三欄、結果 live region、表格六欄、資料說明，以及三個 exact HTTPS 頁尾 URL。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_site_contract.py -q`
Expected: FAIL，因頁面尚不存在。

- [ ] **Step 3: Build the page and interaction controller**

HTML 提供完整無 JS 說明；`app.js` 載入 manifest、lazy-load districts、呼叫純搜尋函式、以 `createElement/textContent` 渲染列、更新分頁與錯誤狀態。CSS 實作深綠／米白視覺、sticky 搜尋區、桌面表格與 720px 以下卡片模式。

- [ ] **Step 4: Run contract and search tests**

Run: `pytest tests/test_site_contract.py -q && node --test tests/site-search.test.mjs`
Expected: PASS。

- [ ] **Step 5: Commit**

`git add site tests/test_site_contract.py && git commit -m "feat: add civic tree search experience"`

### Task 4: 建立 GitHub Pages 自動部署

**Files:**
- Create: `.github/workflows/pages.yml`
- Modify: `tests/test_workflows.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`

**Interfaces:**
- Produces: `pages.yml` 將 `site/` 與生成資料組裝為 Pages artifact；CI 同時執行 pytest 與 Node tests。

- [ ] **Step 1: Write failing workflow contract tests**

斷言 `pages: write`、`id-token: write`、`contents: read`、`push main`、schedule、workflow_dispatch、官方 configure/upload/deploy actions、官方資料擷取與索引建置命令。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_workflows.py -q`
Expected: FAIL，因 `pages.yml` 不存在。

- [ ] **Step 3: Implement workflow and documentation**

工作流建立暫存 raw/processed/output，執行 fetch、normalize、build index，複製 site 靜態資產，最後部署 artifact。CI 加入 Node 20 與 `node --test tests/site-search.test.mjs`。README 加入網站使用、資料更新與 Pages 維運說明。

- [ ] **Step 4: Run workflow and full tests**

Run: `pytest tests -q && node --test tests/site-search.test.mjs`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

`git add .github README.md tests && git commit -m "ci: deploy public search to GitHub Pages"`

### Task 5: 產生真實資料、部署與線上驗證

**Files:**
- Generated: temporary `raw/`、`processed/`、`_site/data/`
- Remote: `gh-pages` branch and GitHub Pages configuration

**Interfaces:**
- Consumes: official `data.taipei` dataset and completed site source。
- Produces: `https://u8901006.github.io/taipei-trees-data/`。

- [ ] **Step 1: Build with real official data**

執行 fetch、normalize、`build_site_data.py`，將 `site/` 複製至暫存 `_site`，確認 manifest `total_count > 0` 且 districts 非空。

- [ ] **Step 2: Run final local verification**

Run: `pytest tests -q`、`node --test tests/site-search.test.mjs`、Ruff checks，以及 HTML/JSON smoke checks。
Expected: 全部 PASS。

- [ ] **Step 3: Commit and push source changes**

提交目前功能分支並推送 origin，使既有 Draft PR 自動更新。

- [ ] **Step 4: Publish the exact build**

建立 `gh-pages` branch、推送已驗證 `_site`，以 GitHub API 設定 Pages source 為 `gh-pages:/`。

- [ ] **Step 5: Verify production**

輪詢 Pages build，確認公開首頁與 `data/manifest.json` HTTP 200、標題正確、`total_count > 0`、三個頁尾 URL 全部存在。
