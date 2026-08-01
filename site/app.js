import { buildGoogleMapsUrl, filterTrees, formatMeasurement, normalizeQuery, paginate } from "./search.mjs";

const PAGE_SIZE = 30;
const number = new Intl.NumberFormat("zh-TW");
const cache = new Map();
let manifest = null;
let schedules = [];
let scheduleCandidateCounts = new Map();
let schedulesById = new Map();
let matchedTrees = [];
let currentPage = 1;

const elements = {
  form: document.querySelector("#tree-search"),
  treeType: document.querySelector("#tree-type"),
  district: document.querySelector("#district"),
  location: document.querySelector("#location"),
  species: document.querySelector("#species"),
  summary: document.querySelector("#results-summary"),
  state: document.querySelector("#result-state"),
  container: document.querySelector("#results-container"),
  body: document.querySelector("#results-body"),
  pagination: document.querySelector("#pagination"),
  previous: document.querySelector("#previous-page"),
  next: document.querySelector("#next-page"),
  pageIndicator: document.querySelector("#page-indicator"),
  scheduleType: document.querySelector("#schedule-type"),
  scheduleQuery: document.querySelector("#schedule-query"),
  scheduleState: document.querySelector("#schedule-state"),
  scheduleList: document.querySelector("#schedule-list"),
};

function formatDate(value) {
  if (!value) return "未提供";
  const match = String(value).match(/^\d{4}-\d{2}-\d{2}/u);
  return match ? match[0].replaceAll("-", "/") : String(value);
}

function setBusy(busy) {
  for (const control of elements.form.elements) control.disabled = busy;
  elements.form.setAttribute("aria-busy", String(busy));
}

function showState(message, kind = "prompt") {
  elements.state.textContent = message;
  elements.state.dataset.kind = kind;
  elements.state.hidden = false;
  elements.container.hidden = true;
  elements.pagination.hidden = true;
}

function addCell(row, label, value) {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  cell.textContent = value;
  row.append(cell);
  return cell;
}

function sourceUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !(url.hostname === "gov.taipei" || url.hostname.endsWith(".gov.taipei"))) {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

function scheduleTypeLabel(value) {
  return value === "park" ? "公園樹木" : "行道樹";
}

function requesterLabel(schedule) {
  const labels = {
    village_chief_recommendation: "里長建議",
    councillor_case: "議員案",
    contractor_report: "承商查報",
    other: "其他依據",
  };
  const type = labels[schedule.requester_type] || schedule.basis || "未提供";
  return `${type}；提報者：${schedule.requester_name || "官方未揭露"}`;
}

function appendDetail(container, term, value) {
  const wrapper = document.createElement("div");
  const label = document.createElement("dt");
  const content = document.createElement("dd");
  label.textContent = term;
  content.textContent = value;
  wrapper.append(label, content);
  container.append(wrapper);
}

function createScheduleCard(schedule, compact = false) {
  const card = document.createElement("article");
  card.className = compact ? "schedule-card schedule-card-compact" : "schedule-card";
  const heading = document.createElement("div");
  heading.className = "schedule-card-heading";
  const badge = document.createElement("span");
  badge.className = `type-badge type-${schedule.category}`;
  badge.textContent = scheduleTypeLabel(schedule.category);
  const title = document.createElement("h3");
  title.textContent = (schedule.locations || []).join("、") || "地點未提供";
  heading.append(badge, title);

  const dates = schedule.start_date === schedule.end_date
    ? formatDate(schedule.start_date)
    : `${formatDate(schedule.start_date)}－${formatDate(schedule.end_date)}`;
  const details = document.createElement("dl");
  details.className = "schedule-details";
  appendDetail(details, "預定日期", dates);
  appendDetail(details, "行政區", (schedule.districts || []).join("、") || "官方未提供");
  appendDetail(details, "預定數量", schedule.planned_count == null ? "官方未提供" : `${number.format(schedule.planned_count)} 棵`);
  appendDetail(details, "施作單位", schedule.work_unit || "官方未提供");
  appendDetail(details, "依據", schedule.basis || "官方未提供");
  appendDetail(details, "提報資訊", requesterLabel(schedule));
  appendDetail(details, "候選樹木", `${number.format(scheduleCandidateCounts.get(schedule.schedule_id) || 0)} 棵可能受影響`);

  const officialUrl = sourceUrl(schedule.source_url);
  const source = document.createElement(officialUrl ? "a" : "span");
  source.className = "official-source";
  source.textContent = officialUrl ? "查看官方行程來源 ↗" : "官方來源連結未通過驗證";
  if (officialUrl) {
    source.href = officialUrl;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
  }
  card.append(heading, details, source);
  return card;
}

function createTreeActions(tree) {
  const cell = document.createElement("td");
  cell.dataset.label = "地圖／行程";
  const actions = document.createElement("div");
  actions.className = "tree-actions";
  const mapUrl = buildGoogleMapsUrl(tree);
  if (mapUrl) {
    const map = document.createElement("a");
    map.className = "map-link";
    map.href = mapUrl;
    map.target = "_blank";
    map.rel = "noopener noreferrer";
    map.textContent = "Google 地圖 ↗";
    map.setAttribute("aria-label", `在 Google 地圖查看${tree.location || tree.species || "這棵樹"}`);
    actions.append(map);
  } else {
    const unavailable = document.createElement("span");
    unavailable.className = "map-unavailable";
    unavailable.textContent = "無定位資料";
    actions.append(unavailable);
  }

  const related = (tree.schedule_ids || []).map((id) => schedulesById.get(id)).filter(Boolean);
  if (related.length) {
    const details = document.createElement("details");
    details.className = "tree-schedules";
    const summary = document.createElement("summary");
    summary.textContent = `可能受修剪影響（${related.length}）`;
    const note = document.createElement("p");
    note.className = "match-note";
    note.textContent = "依路段或公園名稱比對，並非官方逐株施工名單。";
    details.append(summary, note, ...related.map((schedule) => createScheduleCard(schedule, true)));
    actions.append(details);
  }
  cell.append(actions);
  return cell;
}

function renderResults() {
  const page = paginate(matchedTrees, currentPage, PAGE_SIZE);
  currentPage = page.page;
  elements.body.replaceChildren();
  for (const tree of page.items) {
    const row = document.createElement("tr");
    if ((tree.schedule_ids || []).length) row.classList.add("has-schedule");
    addCell(row, "行政區", tree.district || "未提供");
    addCell(row, "路段／公園", tree.location || tree.park_name || "未提供");
    addCell(row, "樹種", tree.species || "未提供");
    addCell(row, "胸徑", formatMeasurement(tree.diameter, "cm"));
    addCell(row, "樹高", formatMeasurement(tree.height, "m"));
    addCell(row, "更新日期", formatDate(tree.updated));
    row.append(createTreeActions(tree));
    elements.body.append(row);
  }
  elements.summary.textContent = `找到 ${number.format(page.total)} 筆結果`;
  elements.state.hidden = true;
  elements.container.hidden = false;
  elements.pagination.hidden = page.pageCount <= 1;
  elements.pageIndicator.textContent = `第 ${page.page} 頁，共 ${page.pageCount} 頁`;
  elements.previous.disabled = page.page <= 1;
  elements.next.disabled = page.page >= page.pageCount;
}

async function fetchJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadDistrict(entry) {
  if (!cache.has(entry.file)) cache.set(entry.file, fetchJson(`./data/${entry.file}`));
  return cache.get(entry.file);
}

async function search() {
  if (!manifest) return;
  const filters = {
    treeType: elements.treeType.value,
    district: elements.district.value,
    location: elements.location.value,
    species: elements.species.value,
  };
  if (!filters.treeType && !filters.district && !filters.location.trim() && !filters.species.trim()) {
    elements.summary.textContent = "尚未查詢";
    showState("請選擇樹木類型或行政區，或輸入道路、公園、樹種開始查詢。");
    return;
  }

  setBusy(true);
  elements.summary.textContent = "正在搜尋公開資料…";
  showState("資料載入中，請稍候…", "loading");
  try {
    const entries = filters.district
      ? manifest.districts.filter((entry) => entry.name === filters.district)
      : manifest.districts;
    const groups = await Promise.all(entries.map(loadDistrict));
    matchedTrees = filterTrees(groups.flat(), filters);
    currentPage = 1;
    if (matchedTrees.length === 0) {
      elements.summary.textContent = "找到 0 筆結果";
      showState("找不到符合條件的樹木。請嘗試較短的地點或樹種關鍵字。", "empty");
    } else {
      renderResults();
    }
  } catch (error) {
    console.error("Tree search failed", error);
    elements.summary.textContent = "資料暫時無法載入";
    showState("目前無法取得樹木資料，請稍後重新整理再試。", "error");
  } finally {
    setBusy(false);
  }
}

function renderSchedules() {
  const type = elements.scheduleType.value;
  const query = normalizeQuery(elements.scheduleQuery.value);
  const visible = schedules.filter((schedule) => {
    if (type && schedule.category !== type) return false;
    const searchable = [
      ...(schedule.districts || []),
      ...(schedule.locations || []),
      schedule.work_unit,
      schedule.basis,
    ].map(normalizeQuery).join(" ");
    return !query || searchable.includes(query);
  });
  elements.scheduleList.replaceChildren(...visible.map((schedule) => createScheduleCard(schedule)));
  elements.scheduleState.hidden = visible.length > 0;
  elements.scheduleState.textContent = schedules.length
    ? "找不到符合條件的修剪行程。"
    : "官方目前未提供可顯示的修剪行程；請參考資料更新時間後再查詢。";
}

async function initializeSchedules() {
  try {
    const [scheduleDocument, matchDocument] = await Promise.all([
      fetchJson(`./data/${manifest.schedule_file}`),
      fetchJson(`./data/${manifest.schedule_matches_file}`),
    ]);
    schedules = Array.isArray(scheduleDocument.schedules) ? scheduleDocument.schedules : [];
    schedulesById = new Map(schedules.map((schedule) => [schedule.schedule_id, schedule]));
    scheduleCandidateCounts = new Map();
    for (const match of matchDocument.matches || []) {
      scheduleCandidateCounts.set(
        match.schedule_id,
        (scheduleCandidateCounts.get(match.schedule_id) || 0) + 1,
      );
    }
    renderSchedules();
  } catch (error) {
    console.error("Schedule load failed", error);
    elements.scheduleState.hidden = false;
    elements.scheduleState.dataset.kind = "error";
    elements.scheduleState.textContent = "修剪行程暫時無法載入；樹木查詢仍可正常使用。";
  }
}

async function initialize() {
  try {
    manifest = await fetchJson("./data/manifest.json");
    document.querySelector("#total-count").textContent = number.format(manifest.total_count);
    document.querySelector("#district-count").textContent = number.format(manifest.district_count);
    document.querySelector("#latest-update").textContent = formatDate(
      manifest.latest_update || manifest.snapshot_date,
    );
    for (const entry of manifest.districts) {
      const option = document.createElement("option");
      option.value = entry.name;
      option.textContent = `${entry.name}（${number.format(entry.count)}）`;
      elements.district.append(option);
    }
    elements.summary.textContent = "資料索引已就緒";
    showState("請選擇樹木類型或行政區，或輸入道路、公園、樹種開始查詢。");
    await initializeSchedules();
  } catch (error) {
    console.error("Manifest load failed", error);
    elements.summary.textContent = "資料暫時無法載入";
    showState("目前無法取得資料索引，請稍後重新整理再試。", "error");
  }
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  search();
});
elements.form.addEventListener("reset", () => {
  window.setTimeout(() => {
    matchedTrees = [];
    currentPage = 1;
    elements.summary.textContent = "尚未查詢";
    showState("請選擇樹木類型或行政區，或輸入道路、公園、樹種開始查詢。");
  }, 0);
});
elements.previous.addEventListener("click", () => {
  currentPage -= 1;
  renderResults();
  elements.summary.scrollIntoView({ behavior: "smooth", block: "center" });
});
elements.next.addEventListener("click", () => {
  currentPage += 1;
  renderResults();
  elements.summary.scrollIntoView({ behavior: "smooth", block: "center" });
});
elements.scheduleType.addEventListener("change", renderSchedules);
elements.scheduleQuery.addEventListener("input", renderSchedules);

initialize();
