# 維運手冊

本手冊說明臺北市樹木透明化資料 repository 的安全操作方式。核心原則是：
原始資料只追加、不覆寫；推論與已確認事實分開；LLM 產物永遠先進人工複核 PR；
來源未設定或失效時，報告必須明確揭露。

## Repository variables 與 secrets

公開的 dataset ID、官方 URL 與 reviewer 帳號放在 GitHub repository variables：

| Variable | 用途 | 未設定時 |
| --- | --- | --- |
| `TAIPEI_STREET_TREES_ID` | 街道樹 dataset ID | 使用 `config/sources.json`；必要來源缺失時同步失敗 |
| `TAIPEI_PROTECTED_TREES_ID` | 受保護樹木 dataset ID 覆寫 | 使用 `config/sources.json` 內的官方資料集與 CSV URL |
| `TAIPEI_PRUNING_SCHEDULE_URL` | 官方修剪行程 URL | 每週 workflow 成功 no-op |
| `TAIPEI_REVIEW_RECORDS_URL` | 審議紀錄索引 | 使用版本控制內的官方預設 URL |
| `TAIPEI_COMMITTEE_RECORDS_URL` | 委員會紀錄索引 | 使用版本控制內的官方預設 URL |
| `REVIEWER` | 預留的人工複核者帳號 | workflow 尚未讀取；改用 ruleset、CODEOWNERS 或手動指派 |

敏感值只能使用 secrets：

| Secret | 用途 | 必要性 |
| --- | --- | --- |
| `DATABASE_URL` | 選用 PostGIS 載入 | 選用；缺少時跳過 DB step |
| `ANTHROPIC_API_KEY` | 選用 PDF 結構化擷取 | workflow 缺少時跳過擷取；PDF 仍可進人工 PR |
| `NOTIFY_WEBHOOK` | 預留通知設定 | 目前沒有通知 step；接線前不得視為已啟用 |

不要把 secret 寫入 `config/sources.json`、workflow command、issue body、artifact 名稱或
測試 fixture。Secret availability gate 只能輸出布林值。

## 第一次手動執行

先在受保護的預設分支設定 required check `verify` 與 reviewer，再依下列順序使用
`workflow_dispatch`：

1. `health-check.yml`：確認來源設定會產生 `reports/health.json`。
2. `daily-opendata.yml`：取得街道樹快照、正規化並產生異常報告。
3. `weekly-schedule.yml`：確認未設定會安全 no-op，或驗證修剪行程快照。
4. `monthly-review.yml`：測試審議 PDF 與 `needs-human-review` PR。
5. 先審核並合併（或關閉）monthly PR，再執行 `quarterly-committee.yml`，避免兩個 PR
   同時修改共用的 `raw/review_meetings/` 與 `extracted/`。
6. `gap-report.yml`：最後依現有證據產生 `reports/gaps.json`。

Pages 第一次發布受保護樹木功能時會建立詳細資料快取；正常排程每次更新新資料、缺漏資料及最久未查詢的 300 筆，約兩週完成一輪。需要低速完整重建時，手動執行：

```powershell
python scripts/fetch_protected_details.py --src processed/protected_trees.parquet --previous-url "" --out processed/protected_tree_details.json --limit 0
```

完整重建會逐筆呼叫官方 API，必須保留預設低併發、請求間隔與重試上限，不可改成大量平行請求。

`config/park_villages.json` 只能加入有官方公園／地址／里界證據的精確對照。每筆需保存來源 URL、查核日期與里長簡介 URL；里長姓名或公開行動電話更動時，以臺北市鄰里服務網目前頁面為準。不要加入里幹事手機、電子郵件或其他未要求公開的聯絡欄位。

首次執行前可在本機跑：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q scripts tests
python -m ruff check scripts tests
python -m ruff format --check scripts tests
```

## 分支保護與人工複核

預設分支應禁止 force push，要求 CI job `verify`、至少一位核准者、所有 review
conversation resolved，並限制直接寫入。Daily、health、weekly、gap 四個 workflow 目前會
直接 push 預設分支，因此 ruleset 只能給 GitHub Actions bot／指定 bot 最小 bypass；不要
給一般使用者或所有 integrations bypass。四個 writer 以共同 `data-sync` concurrency group
序列化；月／季擷取不得 bypass，只能透過至少一位 reviewer 核准的 PR。`REVIEWER`
variable 尚未接入 workflow，請使用 CODEOWNERS、ruleset 或手動指派。

LLM PR 的人工 checklist：

- 案號是否與 PDF 相符。
- 地址是否完整且沒有模型補猜。
- 決議是否保留原意。
- 樹木株數是否為原文支持的非負整數。
- 會議日期是否為有效 `YYYY-MM-DD`。
- 頁碼是否正確。
- `quote_snippet` 是否為該頁精確、短而足夠的引文。

任何一項不能定位原文時，該欄位保持 null；不要以人工猜測補入自動化產物。

## 異常與 issue 處理

`reports/anomalies.json` 的訊號不是已確認的移除事實。收到 anomaly issue 時：

1. 確認來源與 manifest hash 未損壞。
2. 比較 `processed/snapshots/<source>/` 下相鄰日期的 Parquet。
3. 欄位變更先修 ETL；大量消失先排除來源故障。
4. 單株消失保留 `confidence: inferred`，再查閱審議紀錄或向機關查證。
5. 以 label `anomaly-detected` 篩選 open issues，再比對 exact title
   `臺北樹木資料異常：需要人工查證`；只有不存在時才建立。

## 來源失效與來源更新

`health.json` 會保留第一次失效的 `unavailable_since`；恢復後才清除。前端或
`/watch/gaps` 消費者應直接呈現「本資料自 YYYY-MM-DD 起未能更新」，不可用舊快照冒充
最新資料。

更新 dataset ID 或 URL 時：

1. Open Data 只採用 `https://data.taipei`（預設 443）；redirect 每一跳都必須維持相同
   官方 HTTPS 邊界。其他來源只採用可公開查證的臺北市官方 HTTPS。
2. 先在 PR 修改 `config/sources.json` 或 repository variable。
3. 本機跑 config、health、fetch 與 workflow tests。
4. 確認新來源不含 userinfo、fragment 或 token 類 query key。
5. 不改寫既有 `raw/` 快照；新來源從新日期開始追加。

## Push、immutable conflict 與復原

Direct-writer workflow 在寫入前及 push 前執行 pull/rebase。若 push 仍失敗：

1. 不要 force push。
2. 重新執行 workflow，讓它在最新分支上重建可重建產物。
3. 若同一不可變路徑內容不同，停止並檢查官方來源、時區日期與 concurrent run。
4. manifest 缺失、hash/長度不符或 orphan snapshot 均視為損壞；不要刪除證據來讓 job 通過。
5. 從可信 commit/備份還原 pair，再重新執行衍生步驟。

## 保留、備份與大型檔案

`raw/` 是稽核來源，原則上永久保留並以 Git commit hash 引用。`processed/` 可用同一 raw
與固定程式版本重新產生；`reports/` 含 clock 與外部 health state，重跑未必 bit-for-bit
相同。`extracted/` 依賴模型/OCR且承載人工審核脈絡，不可假定可重現，必須連同 raw 與
review history 一起保留、備份。定期建立 repository mirror。

當單檔接近 GitHub 建議上限、repository clone 成本明顯上升，或 PDF 累積量過大時，再以
可驗證 hash/index 評估 Git LFS 或 R2；遷移前不得破壞既有 commit 的可追溯性。

## GitHub 排程 60 日限制

Public repository 若約 60 天無活動，GitHub 可能停用 scheduled workflows。即使平常有每日
資料 commit，維護者仍應監測 Actions；停用後先人工檢查來源與分支狀態，再重新啟用並按
「第一次手動執行」順序補跑，不要假定漏失期間資料完整。

## PostGIS 與通知

PostGIS 是選用的發布副本，不是稽核真相來源。Daily workflow 先完成 Git commit/push，
並確認 remote revision 等於本機 HEAD，才從該 revision 載入資料庫。Loader 在同一
transaction 完成 staging/upsert 與來源缺失列清除；失敗時 rollback，但不回滾或改寫
已發布的 Git/raw snapshot。設定 `DATABASE_URL` 前，先在測試資料庫驗證 schema 與
transaction。`NOTIFY_WEBHOOK` 目前只是預留 secret，沒有 workflow/script 會使用；
未來若接線，只能傳固定摘要與 repository 連結，不得傳環境變數、來源內容或模型回應。

月／季 workflow 會先安裝 Poppler、Tesseract 與 `chi_tra` 語言包。缺少
`ANTHROPIC_API_KEY` 時會直接跳過 extraction，但新 PDF 仍可由
`create-pull-request` 進入人工 PR。只有直接執行 `extract_cases.py` 且沒有 key 時，CLI
才會為尚未處理的 PDF 記錄 `missing_api_key` failure，且不執行 OCR 或模型。

## 樹種照片快取

GitHub Pages 發布流程會先移除官方學名後方的命名者字串，再查詢 Wikimedia Commons；無學名或 Commons 無結果時，以農業部 TBN Open API 將精確中文名稱對應為標準學名後補查。若 Commons 仍無精確圖片，使用 TBN-DP「臺灣維管束植物調查及物候觀察」中同一 taxon UUID、具公開媒體授權的已鑑定照片；最後才查詢中文維基百科縮圖。Commons 搜尋結果必須同時包含完整屬名與種小名，不能只因同屬就採用。快取保留圖片來源、作者、授權及擷取時間，並在每次發布優先補齊缺圖項目。查無可核實照片會記為 `unavailable`，前端不得拿相似名稱樹種替代。

## 前端 `/watch/gaps` 契約

前端讀取 `reports/health.json` 顯示每個來源的即時狀態與失效起日；讀取
`reports/gaps.json` 顯示證據路徑、整日 age、stale/missing/pending/failure 訊號。前端不可
自行把 `not_configured` 轉成 available，也不可隱藏 pending extraction 或來源失效。
