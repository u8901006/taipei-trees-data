# 臺北樹木資料管線

本專案將臺北市公開樹木資料、保護樹、修剪時程及審議／委員會紀錄整理為可重現、可追溯的資料管線。原始快照與處理結果會保留來源設定與取得日期；未設定或不可用的選用來源也必須明確呈現，不以推測資料補足。

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

資料集 ID、可公開的 URL 與人工複核者均應設為 GitHub repository variables；不得把 token 或密碼放入 `sources.json`、提交紀錄或日誌。可覆寫來源的 variables 如下：

| Variable | 用途 |
| --- | --- |
| `TAIPEI_STREET_TREES_ID` | 街道樹 Open Data dataset ID |
| `TAIPEI_PROTECTED_TREES_ID` | 保護樹 Open Data dataset ID |
| `TAIPEI_PRUNING_SCHEDULE_URL` | 官方修剪時程 URL |
| `TAIPEI_REVIEW_RECORDS_URL` | 官方樹木審議紀錄索引 URL |
| `TAIPEI_COMMITTEE_RECORDS_URL` | 官方委員會紀錄索引 URL |
| `REVIEWER` | 預留，workflow 尚未接線；使用 CODEOWNERS、ruleset 或手動指派 |

唯一允許的 secrets 為 `DATABASE_URL`、`ANTHROPIC_API_KEY` 與預留但尚未接線的
`NOTIFY_WEBHOOK`。程式不得輸出這些值或將其寫入報告。

## 資料來源限制

預設街道樹資料集 ID 為 `7a49d00c-a5ff-4a6b-be9e-aaa6dc1ff7e8`。審議與委員會紀錄預設索引為臺北市文化局的公開頁面。保護樹與修剪時程目前可維持為未設定；這是誠實的狀態，不代表資料不存在。資料發布單位可能調整欄位、網址、可下載格式或歷史資料，因此每次擷取均須保留來源與快照日期。

## 授權與使用注意

程式碼與資料的授權狀態須分別判斷。本 repository 尚未新增程式碼授權檔，未取得明確授權前不得假定可再散布。政府公開資料與文化局網頁內容仍受各來源公告的使用條款、著作權與個資規範拘束；發布或再利用前請查核原始來源的最新授權條件並保留歸屬資訊。

## CLI 範例

```powershell
python scripts/fetch_opendata.py --out raw/open_data/
python scripts/normalize.py --raw raw/open_data/ --out processed/
python scripts/detect_anomalies.py --processed processed/ --out reports/anomalies.json
python scripts/health_check.py --out reports/health.json
python scripts/fetch_schedule.py --out raw/pruning_schedules/
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
