# 維運手冊

本手冊說明臺北市樹木透明化資料 repository 的安全操作方式。核心原則是：
原始資料只追加、不覆寫；推論與已確認事實分開；LLM 產物永遠先進人工複核 PR；
來源未設定或失效時，報告必須明確揭露。

## Repository variables 與 secrets

公開的 dataset ID、官方 URL 與 reviewer 帳號放在 GitHub repository variables：

| Variable | 用途 | 未設定時 |
| --- | --- | --- |
| `TAIPEI_STREET_TREES_ID` | 街道樹 dataset ID | 使用 `config/sources.json`；必要來源缺失時同步失敗 |
| `TAIPEI_PROTECTED_TREES_ID` | 保護樹 dataset ID | `not_configured`，不宣稱已有涵蓋 |
| `TAIPEI_PRUNING_SCHEDULE_URL` | 官方修剪行程 URL | 每週 workflow 成功 no-op |
| `TAIPEI_REVIEW_RECORDS_URL` | 審議紀錄索引 | 使用版本控制內的官方預設 URL |
| `TAIPEI_COMMITTEE_RECORDS_URL` | 委員會紀錄索引 | 使用版本控制內的官方預設 URL |
| `REVIEWER` | 人工複核者帳號 | PR 仍可建立，但須由 repository 維護者指派 |

敏感值只能使用 secrets：

| Secret | 用途 | 必要性 |
| --- | --- | --- |
| `DATABASE_URL` | 選用 PostGIS 載入 | 選用；缺少時跳過 DB step |
| `ANTHROPIC_API_KEY` | 選用 PDF 結構化擷取 | 選用；缺少時保留 pending/failure，不呼叫模型 |
| `NOTIFY_WEBHOOK` | 選用固定摘要通知 | 選用；不得出現在 log、artifact 或 commit |

不要把 secret 寫入 `config/sources.json`、workflow command、issue body、artifact 名稱或
測試 fixture。Secret availability gate 只能輸出布林值。

## 第一次手動執行

先在受保護的預設分支設定 required checks 與 reviewer，再依下列順序使用
`workflow_dispatch`：

1. `health-check.yml`：確認來源設定會產生 `reports/health.json`。
2. `daily-opendata.yml`：取得街道樹快照、正規化並產生異常報告。
3. `weekly-schedule.yml`：確認未設定會安全 no-op，或驗證修剪行程快照。
4. `monthly-review.yml`：測試審議 PDF 與 `needs-human-review` PR。
5. `quarterly-committee.yml`：測試委員會分類與人工複核 PR。
6. `gap-report.yml`：最後依現有證據產生 `reports/gaps.json`。

首次執行前可在本機跑：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q scripts tests
python -m ruff check scripts tests
python -m ruff format --check scripts tests
```

## 分支保護與人工複核

預設分支應禁止 force push，要求 CI、至少一位核准者、所有 review conversation resolved，
並限制直接寫入。資料 bot 的 direct-writer workflows 以共同 `data-sync` concurrency group
序列化；月／季擷取只能透過 PR 合併。

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
2. 比較前後兩份 `processed/*/history.parquet`。
3. 欄位變更先修 ETL；大量消失先排除來源故障。
4. 單株消失保留 `confidence: inferred`，再查閱審議紀錄或向機關查證。
5. 以固定 title/label 維持單一開放 issue，避免重複告警。

## 來源失效與來源更新

`health.json` 會保留第一次失效的 `unavailable_since`；恢復後才清除。前端或
`/watch/gaps` 消費者應直接呈現「本資料自 YYYY-MM-DD 起未能更新」，不可用舊快照冒充
最新資料。

更新 dataset ID 或 URL 時：

1. 只採用可公開查證的臺北市官方 HTTPS 來源。
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

`raw/` 是稽核來源，原則上永久保留並以 Git commit hash 引用。`processed/`、`extracted/`
與 `reports/` 可由原始資料重建，但仍提交以供網站與審查使用。定期建立 repository mirror。

當單檔接近 GitHub 建議上限、repository clone 成本明顯上升，或 PDF 累積量過大時，再以
可驗證 hash/index 評估 Git LFS 或 R2；遷移前不得破壞既有 commit 的可追溯性。

## GitHub 排程 60 日限制

Public repository 若約 60 天無活動，GitHub 可能停用 scheduled workflows。即使平常有每日
資料 commit，維護者仍應監測 Actions；停用後先人工檢查來源與分支狀態，再重新啟用並按
「第一次手動執行」順序補跑，不要假定漏失期間資料完整。

## PostGIS 與通知

PostGIS 是選用的發布副本，不是稽核真相來源。先在 staging 驗證 schema 與 transaction，
再設定 `DATABASE_URL`；載入失敗不應修改 raw snapshot。Webhook 只傳固定摘要與 repository
連結，不傳環境變數、來源內容或模型回應。

## 前端 `/watch/gaps` 契約

前端讀取 `reports/health.json` 顯示每個來源的即時狀態與失效起日；讀取
`reports/gaps.json` 顯示證據路徑、整日 age、stale/missing/pending/failure 訊號。前端不可
自行把 `not_configured` 轉成 available，也不可隱藏 pending extraction 或來源失效。

