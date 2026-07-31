# 資料契約

所有 JSON 為 UTF-8、結尾換行、穩定 key ordering；時間為含時區 ISO 8601；日期為
`YYYY-MM-DD`；SHA-256 為 64 個小寫十六進位字元。Reader 對未知欄位、重複 JSON key、
NaN/Infinity、錯誤型別與不安全路徑採 fail-closed。

## 原始開放資料

路徑：`raw/open_data/<source>/<YYYY-MM-DD>.csv.gz`，相鄰 manifest 為
`<YYYY-MM-DD>.manifest.json`。

Manifest 欄位：

- `source_name`
- `dataset_id`
- `resource_id`
- `original_url`
- `retrieved_at`
- `uncompressed_byte_length`
- `sha256`（解壓後 CSV bytes）

同一路徑只能建立一次；相同 bytes 為 `unchanged`，不同 bytes 為 immutable conflict。
Manifest 必須與實體 gzip 解壓內容的長度/hash 相符。

## 修剪行程

路徑：`raw/pruning_schedules/<臺北日期>/pruning_schedule.<ext>` 與
`pruning_schedule.manifest.json`。副檔名由受允許 MIME 決定（PDF、JSON、CSV 或文字）。

Manifest exact fields 為 `schema_version`、`source_url`、`retrieved_at`、`content_type`、
`byte_length`、`sha256`。PDF 必須有 `%PDF-` magic；JSON 必須嚴格可解析；文字不得為空、
NUL 或 HTML 偽裝。Snapshot/manifest orphan、內容類型改變或同日不同內容一律衝突。

## 會議 PDF

路徑：`raw/review_meetings/<YYYY-MM>/<安全標題>.pdf` 與相鄰 manifest。Manifest 記錄：

- `schema_version`
- `title`
- `published_date`
- `detail_url`
- `attachment_url`
- `retrieved_at`
- `byte_length`
- `sha256`

URL 必須是臺北市官方 HTTPS。`published_date` 決定月份 partition；`retrieved_at` 可晚於發布
日但不可早於發布或位於未來。PDF magic、byte length 與 hash 必須重驗。審議與委員會依
標題 taxonomy 分開，不能用同一 PDF 冒充兩種涵蓋。

## 正規化資料與事件

每個來源在 `processed/<source>/` 產生：

- `current.parquet`：最新正規化樹籍。
- `history.parquet`：按 snapshot date 保留歷史列。
- `events.jsonl`：由相鄰快照推得的事件。
- `metadata.json`：schema/version、來源與產出資訊。

樹籍 ID 是追蹤主鍵。由資料消失推得的 removal 只能標記 `confidence: inferred`；不能描述為
已確認砍除。受保護樹清單刪除屬最高嚴重度訊號，仍須人工查證。

## 異常報告

`reports/anomalies.json` 包含 `schema_version`、`generated_at`、`found`、`summary`、
`detail` 與排序後 `anomalies`。Rule/status/severity 使用程式中的固定 enum。報告是品質與
追蹤訊號，不是行政事實認定。

## 來源健康

`reports/health.json`：

- root：`schema_version`、`generated_at`、`sources`
- source：`name`、`kind`、`required`、`status`、`checked_at`、`reason`、
  `unavailable_since`

Status 只能是：

- `available`：`reason` 與 `unavailable_since` 均為 null。
- `unavailable`：固定失敗 reason，且保留第一次 `unavailable_since`。
- `not_configured`：`reason: source_not_configured`，`unavailable_since: null`。

恢復 available 時清除失效起日；來源 kind 改變時不能沿用舊 continuity。

## 透明度缺口

`reports/gaps.json` 包含 `schema_version`、`generated_at`、`stale_after_days`、`summary`、
排序後 `sources` 與 `gaps`。每個 source 提供 status、required、repository-relative
`evidence_paths`、`snapshot_age_days` 與繁中 message。

Gap codes 至少涵蓋：

- `source_unavailable`
- `source_not_configured`
- `stale_snapshot`
- `missing_protected_trees`
- `missing_pruning_schedule`
- `pending_extraction_review`
- `extraction_failures`

證據檔案必須在 repository base 內且通過 manifest/hash/日期/schema 驗證；symlink escape、
未來時間、損壞 gzip/PDF 與絕對路徑不得成為 evidence。

## LLM 擷取 case

路徑：`extracted/<與來源相同相對父目錄>/<stem>.json`。Root exact fields：

- `schema_version`
- `source_pdf`（repository-relative）
- `source_sha256`
- `model`
- `review_status`（自動化永遠為 `pending`）
- `fields`

`fields` exact names：`case_number`、`address`、`decision`、`tree_count`、
`meeting_date`。每欄 exact object：

```json
{
  "value": null,
  "page": null,
  "quote_snippet": null,
  "confidence": null
}
```

非 null 欄位必須有 1-based 有效頁碼、該頁 normalized exact-substring quote，以及
`high|medium|low` confidence。Quote raw/normalized 最長 500 字，且不得接近整頁（90%）。
`tree_count` 是非負整數（bool 不可）；`meeting_date` 是有效 `YYYY-MM-DD`。任一證據條件
失敗就把整欄設 null，不截斷或猜測。

`extracted/extraction_failures.json` root 為 `schema_version`、`generated_at`、排序去重的
`failures`；每筆只有 relative `source_pdf`、固定 field 與固定 reason。不得保存 API key、
絕對本機路徑、完整頁面或模型原始回應。

## PostGIS

資料庫是 processed 資料的交易式副本。Loader 使用 staging/upsert 並在單一 transaction
完成；連線或 schema 失敗時 rollback。資料庫內容不得反向覆寫 raw snapshots。

## Schema 演進

目前 artifacts 使用明確 `schema_version`。不相容變更必須：

1. 提升 schema version。
2. 同步更新 writer、strict reader、tests 與本文件。
3. 提供 migration 或保留舊 reader；不可靜默重新解讀舊欄位。
4. 不改寫既有 raw artifacts。

新增 optional source 不代表已有資料；在設定與證據齊備前必須保持 `not_configured` 或
對應 missing gap。

