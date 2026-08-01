# 資料契約

Writer 產生的 JSON 為 UTF-8、結尾換行、穩定 key ordering；時間為含時區 ISO 8601；
日期為 `YYYY-MM-DD`；SHA-256 為 64 個小寫十六進位字元。Health、gap、schedule 與
extraction 等 strict consumers 會拒絕其契約中的未知欄位、重複 JSON key、NaN/Infinity、
錯誤型別與不安全路徑；Open Data manifest 與 normalized schema reader 目前沒有同等的
duplicate/extra-key 全域保證，應依各節列出的實際驗證行為使用。

## 原始開放資料

路徑：`raw/open_data/<source>/<YYYY-MM-DD>.csv.gz`，manifest 為同目錄的
`<YYYY-MM-DD>.json`。

Manifest 欄位：

- `source_name`
- `dataset_id`
- `resource_id`
- `original_url`
- `retrieved_at`
- `uncompressed_byte_length`
- `sha256`（解壓後 CSV bytes）

`dataset_id` 與 `resource_id` 可為 null。此 manifest 目前沒有 `schema_version`。同一路徑
只能建立一次；解壓後 CSV bytes 不同，或既有 manifest 的六個重跑比對欄位
`source_name`、`dataset_id`、`resource_id`、`original_url`、
`uncompressed_byte_length`、`sha256` 任一與本次不同，才是 immutable conflict；上述
bytes/欄位全相同為 `unchanged`。`retrieved_at` 記錄首次成功封存時間，不參與重跑
一致性比較。現行 reader 不拒絕額外 manifest 欄位、不同 key ordering 或縮排。
Manifest 契約欄位仍必須與實體 gzip 解壓內容的長度/hash 相符。

## 修剪行程

路徑：`raw/pruning_schedules/<臺北日期>/pruning_index.html`、
`pruning_street.html`、`pruning_park.html` 與各自相鄰的 `.manifest.json`。舊式單檔來源仍可
保存為 `pruning_schedule.<ext>`。副檔名由受允許 MIME 決定（HTML、PDF、JSON、CSV 或文字）。

Manifest exact fields 為 `schema_version`（integer `1`）、`source_url`、`retrieved_at`、
`content_type`、`byte_length`、`sha256`。HTML 必須具有頁面結構及連結或表格；PDF 必須有
`%PDF-` magic；JSON 必須嚴格可解析；文字不得為空、NUL 或 HTML 偽裝。
Snapshot/manifest orphan、內容類型改變或同日不同內容一律衝突。三份頁面皆成功解析後，才會
原子更新 `processed/pruning_schedule.json`。

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

`schema_version` 是 integer `1`。同標題有多個附件時，第二份起使用
`<安全標題>__2.pdf`、`__3.pdf` 等 suffix。

URL 必須是臺北市官方 HTTPS。`published_date` 決定月份 partition；`retrieved_at` 可晚於發布
日但不可早於發布或位於未來。PDF magic、byte length 與 hash 必須重驗。審議與委員會依
標題 taxonomy 分開，不能用同一 PDF 冒充兩種涵蓋。

## 正規化資料與事件

每個日期快照產生
`processed/snapshots/<source>/<YYYY-MM-DD>.parquet`，相鄰 metadata 為
`<YYYY-MM-DD>.schema.json`。Metadata exact fields：
`canonical_headers`、`encoding`、`original_headers`、`row_count`、`sha256`。

正規化 Parquet canonical 15 欄依序為：
`tree_id`、`tree_type`、`district`、`location`、`location_note`、`park_name`、`species`、`diameter_cm`、
`height_m`、`survey_date`、`twd97_x`、`twd97_y`、`updated_at`、`source`、
`snapshot_date`。

`tree_id` 必填、唯一，輸出依 `tree_id` 使用穩定排序。數值欄無法解析時為 null；日期欄
正規化為 ISO date 或 null。相鄰 `.schema.json` 目前沒有 `schema_version`。

最新街道樹副本為 `processed/trees.parquet`；公園樹副本為
`processed/park_trees.parquet`；有設定保護樹時另有 `processed/protected_trees.parquet`。
所有推論事件統一追加至
`processed/tree_events.jsonl`，不存在 `current.parquet`、`history.parquet` 或
`metadata.json`。

事件 exact 6 欄為 `event_type`、`confidence`、`tree_id`、`source`、
`previous_snapshot_date`、`current_snapshot_date`；目前 enum 固定為
`event_type: removal`、`confidence: inferred`。樹籍 ID 是追蹤主鍵；不得描述為已確認
砍除。受保護樹清單刪除屬最高嚴重度訊號，仍須人工查證。

## 異常報告

`reports/anomalies.json` 使用 string `schema_version: "1.0"`，exact root fields：
`schema_version`、`generated_at`、`found`、
`summary`、`detail`、`anomalies`。每個 anomaly exact base fields：
`severity`、`source`、`rule`、`title`、`detail`；`missing_tree` 另含 `tree_id`。
Severity enum：`critical|high|medium|low`。Rule enum：
`count_drop|missing_tree|schema_change|repeated_raw_hash`。Anomaly item 沒有 `status`
欄位。報告是品質與追蹤訊號，不是行政事實認定。

## 來源健康

`reports/health.json` 使用 string `schema_version: "1.0"`：

- root：`schema_version`、`generated_at`、`sources`
- source：`name`、`kind`、`required`、`status`、`checked_at`、`reason`、
  `unavailable_since`

Status 只能是：

- `available`：`reason` 與 `unavailable_since` 均為 null。
- `unavailable`：固定失敗 reason，且保留第一次 `unavailable_since`。
- `not_configured`：`reason: source_not_configured`，`unavailable_since: null`。

Reason enum exact 為 `probe_failed|redirect_rejected|source_not_configured`；前兩者只適用
`unavailable`，後者只適用 `not_configured`。

恢復 available 時清除失效起日；來源 kind 改變時不能沿用舊 continuity。

## 透明度缺口

`reports/gaps.json` 使用 string `schema_version: "1.0"`，exact root fields：
`schema_version`、`generated_at`、
`stale_after_days`、`summary`、`sources`、`gaps`。Summary exact fields：
`source_count`、`available_sources`、`unavailable_sources`、
`not_configured_sources`、`gap_count`。Source exact fields：
`name`、`status`、`required`、`evidence_paths`、`snapshot_age_days`、`message`。
Gap exact fields：`code`、`source`、`count`、`age_days`、`evidence_paths`、`message`。

Gap code enum exact 為：

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
- `source_pdf`（相對於 extraction input root `raw/review_meetings/`，例如
  `YYYY-MM/foo.pdf`；不是 repository-relative）
- `source_sha256`
- `model`
- `review_status`（自動化永遠為 `pending`）
- `fields`

Case 與 `extraction_failures.json` 均使用 string `schema_version: "1.0"`。
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

`extracted/extraction_failures.json` exact root fields 為 `schema_version`、
`generated_at`、`failures`；每筆 exact fields 為 `source_pdf`、`field`、`reason`。
Field enum：`case_number|address|decision|tree_count|meeting_date|__root__`。Reason enum：
`empty_value|invalid_confidence|invalid_field_set|invalid_field_shape|`
`invalid_meeting_date|invalid_null_contract|invalid_tree_count|invalid_value_type|`
`malformed_model_json|missing_api_key|model_error|page_out_of_range|page_required|`
`pdf_extraction_error|quote_not_exact|quote_required|quote_too_broad|quote_too_long`。
Failure 會穩定排序去重；不得保存 API key、絕對本機路徑、完整頁面或模型原始回應。

## PostGIS

資料庫是已發布 processed 資料的交易式副本，不是 Git 稽核真相來源。Daily workflow
必須先成功發布 Git revision，再確認 remote revision 與本機 HEAD 相同後才載入。
Loader 在單一 transaction 先 staging/upsert，再依本次 Parquet 的 `source` 刪除
staging 中已不存在的舊列；任一步驟失敗皆 rollback。固定檔名 `trees.parquet` 與
`protected_trees.parquet` 的有效空快照，分別代表清空 `street_trees` 與
`protected_trees`；其他檔名的空快照因無法確認來源而拒絕。資料庫內容不得反向覆寫
raw snapshots。目錄模式固定先載入 `trees.parquet`；若 `protected_trees.parquet`
存在，也必須接續載入並彙總結果。`--parquet` 則只載入明確指定的單一副本。

## Schema 演進

Schedule/review manifest 使用 integer `schema_version: 1`；anomaly、health、gap、case 與
extraction failure 使用 string `"1.0"`。Open Data manifest、normalized `.schema.json`
與 Parquet 目前未版本化。對未版本化 closed schema 的不相容變更，必須先引入版本欄位與
migration。其餘不相容變更必須：

1. 提升 schema version。
2. 同步更新 writer、strict reader、tests 與本文件。
3. 提供 migration 或保留舊 reader；不可靜默重新解讀舊欄位。
4. 不改寫既有 raw artifacts。

新增 optional source 不代表已有資料；在設定與證據齊備前必須保持 `not_configured` 或
對應 missing gap。

## 公開網站資料

`site/data/manifest.json` 使用 integer `schema_version: 2`，包含總筆數、行政區分區、
`type_counts`、來源更新日期、修剪行程筆數及 `schedules.json`、
`schedule_matches.json` 檔名。公開樹木 ID 為 `<street|park>:<TreeID>`，避免兩種官方資料的
TreeID 相撞。

行政區 JSON 的樹木欄位為 `id`、`tree_type`、`district`、`location`、`park_name`、
`species`、`diameter`、`height`、`updated`、`latitude`、`longitude`、`schedule_ids`。
座標由 EPSG:3826 轉成 EPSG:4326，只有臺北合理範圍內的有限數值才公開；否則經緯度皆為
null。

`schedules.json` 保留官方行程證據欄位；`schedule_matches.json` 只保存可解釋的候選關聯。
候選關聯不得依 `planned_count` 截斷，也不得把地理位置或施作單位推論成里長、議員或提報承商
姓名。發布驗證器會重驗分區筆數、類型總數、TreeID 唯一性、行程筆數及所有配對參照。
