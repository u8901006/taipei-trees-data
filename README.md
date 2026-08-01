# 臺北樹木資料管線

本專案將臺北市公開行道樹、公園樹木、受保護樹木、修剪時程及審議／委員會紀錄整理為可重現、可追溯的資料管線。受保護樹木另同步官方登錄樹齡、推估植栽年、檔案照片、故事與管理資訊；原始快照與處理結果會保留來源設定與取得日期，不以推測資料補足。

## 目錄慣例

- `config/`：版本控制的資料來源設定；`sources.json` 是所有來源的唯一設定入口。
- `scripts/`：可測試的 Python 管線程式。
- `tests/`：離線測試與固定測試資料。
- `raw/`：不可變的原始下載快照（應提交，供稽核）。
- `processed/`：由原始快照產生的結構化資料（應提交）。
- `extracted/`：附有頁碼與引文證據的擷取結果（應提交）。
- `reports/`：健康狀態、缺口與異常報告（應提交）。

## 本機設定與指令

本專案要求 Python 3.12。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q scripts tests
python -m ruff check scripts tests
python -m ruff format --check scripts tests
```

只驗證來源設定時：

```powershell
python -m pytest tests/test_config.py -q
```

## Repository variables 與 secrets

公開 dataset ID 與 URL 可用 GitHub repository variables 覆寫；`REVIEWER` 目前僅預留。
不得把 token 或密碼放入 `sources.json`、提交紀錄或日誌：

| Variable | 用途 |
| --- | --- |
| `TAIPEI_STREET_TREES_ID` | 行道樹 Open Data dataset ID |
| `TAIPEI_PARK_TREES_URL` | 公園樹木 CSV URL |
| `TAIPEI_PROTECTED_TREES_ID` | 保護樹 Open Data dataset ID |
| `TAIPEI_PRUNING_SCHEDULE_URL` | 官方修剪時程 URL |
| `TAIPEI_REVIEW_RECORDS_URL` | 官方樹木審議紀錄索引 URL |
| `TAIPEI_COMMITTEE_RECORDS_URL` | 官方委員會紀錄索引 URL |
| `REVIEWER` | 預留，workflow 尚未接線；使用 CODEOWNERS、ruleset 或手動指派 |

唯一允許的 secrets 為 `DATABASE_URL`、`ANTHROPIC_API_KEY` 與預留但尚未接線的
`NOTIFY_WEBHOOK`。程式不得輸出這些值或將其寫入報告。

## 資料來源限制

預設行道樹資料集 ID 為 `7a49d00c-a5ff-4a6b-be9e-aaa6dc1ff7e8`，公園樹木與修剪行程則使用臺北市政府公開的 CSV 與公園處行程頁面。審議與委員會紀錄預設索引為臺北市文化局的公開頁面。資料發布單位可能調整欄位、網址、可下載格式或歷史資料，因此每次擷取均須保留來源與快照日期；來源未揭露的提報者姓名不得推測。

## 授權與使用注意

程式碼與資料的授權狀態須分別判斷。本 repository 尚未新增程式碼授權檔，未取得明確授權前不得假定可再散布。政府公開資料與文化局網頁內容仍受各來源公告的使用條款、著作權與個資規範拘束；發布或再利用前請查核原始來源的最新授權條件並保留歸屬資訊。

## CLI 範例

```powershell
python scripts/fetch_opendata.py --out raw/open_data/
python scripts/normalize.py --raw raw/open_data/ --out processed/
python scripts/detect_anomalies.py --processed processed/ --out reports/anomalies.json
python scripts/health_check.py --out reports/health.json
python scripts/fetch_schedule.py --out raw/pruning_schedules/ --processed-out processed/pruning_schedule.json
python scripts/gap_report.py --health reports/health.json --out reports/gaps.json
python scripts/crawl_review_records.py --kind review --out raw/review_meetings/
python scripts/crawl_review_records.py --kind committee --out raw/review_meetings/
python scripts/extract_cases.py --in raw/review_meetings/ --out extracted/
python scripts/load_postgis.py --src processed/
```

原始資料採 append-only；推論事件保留 `inferred`；自動擷取一律為 `pending`，只有人工核對
頁碼與精確引文後才可合併。更完整的設定、復原與 schema 規則如下。

## 維運與資料契約

- [維運手冊](docs/operations.md)
- [資料契約](docs/data-contract.md)
# 市民行道樹與公園樹木查詢網站

公開網站：[https://u8901006.github.io/taipei-trees-data/](https://u8901006.github.io/taipei-trees-data/)

網站提供手機友善的結果卡片與桌面表格，可依樹木類型、行政區、路段／公園或樹種搜尋真實公開資料。每筆具有效座標的樹木可開啟 Google Maps；受保護樹木可查看官方樹齡、照片與故事，點選樹種可查看臺北市統計及權威資料庫連結。修剪案件若為里長建議且地點有官方里別證據，會列出現任里長、公開行動電話及簡介來源。網站不需要後端服務，瀏覽器會依行政區載入建置時產生的 JSON 索引。

`.github/workflows/pages.yml` 會在 `main` 更新、每日臺北時間 04:30 或手動觸發時，從臺北市官方來源重新擷取資料、正規化、建立搜尋索引並發布至 GitHub Pages。修剪行程另於每日臺北時間 09:20 建立可稽核快照。資料更新日期與缺漏欄位會如實呈現；網站不推測或補造來源未提供的提報者姓名，行程與樹木的關聯僅標示為保守文字比對所得的「可能受影響樹木」。

部署前會檢查至少 50,000 筆、至少 3,000 筆受保護樹木、完整 12 個行政區、各分區檔案筆數與 manifest 總數一致；若官方來源回傳空白或嚴重縮水資料，工作流程會停止並保留上一個正常網站版本。詳細資料尚未輪替同步時顯示「詳細資料同步中」；已同步但官方沒有該欄位時顯示「官方未提供」。

## 本機預覽

先準備 `processed/trees.parquet`、`processed/park_trees.parquet`、`processed/protected_trees.parquet`、`processed/protected_tree_details.json` 與 `processed/pruning_schedule_enriched.json`，再執行：

```powershell
python scripts/build_site_data.py --src processed/trees.parquet --park-src processed/park_trees.parquet --protected-src processed/protected_trees.parquet --protected-details processed/protected_tree_details.json --schedule processed/pruning_schedule_enriched.json --out site/data
python -m http.server 8000 --directory site
```

開啟 `http://localhost:8000`。請勿直接以檔案方式開啟 `index.html`，瀏覽器會阻擋模組與 JSON 載入。
