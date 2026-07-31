import { filterTrees, formatMeasurement, paginate } from "./search.mjs";

const PAGE_SIZE = 30;
const number = new Intl.NumberFormat("zh-TW");
const cache = new Map();
let manifest = null;
let matchedTrees = [];
let currentPage = 1;

const elements = {
  form: document.querySelector("#tree-search"),
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
}

function renderResults() {
  const page = paginate(matchedTrees, currentPage, PAGE_SIZE);
  currentPage = page.page;
  elements.body.replaceChildren();
  for (const tree of page.items) {
    const row = document.createElement("tr");
    addCell(row, "行政區", tree.district || "未提供");
    addCell(row, "路段", tree.location || "未提供");
    addCell(row, "樹種", tree.species || "未提供");
    addCell(row, "胸徑", formatMeasurement(tree.diameter, "cm"));
    addCell(row, "樹高", formatMeasurement(tree.height, "m"));
    addCell(row, "更新日期", formatDate(tree.updated));
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
    district: elements.district.value,
    location: elements.location.value,
    species: elements.species.value,
  };
  if (!filters.district && !filters.location.trim() && !filters.species.trim()) {
    elements.summary.textContent = "尚未查詢";
    showState("請選擇行政區，或輸入路段、樹種開始查詢。");
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
      showState("找不到符合條件的樹木。請嘗試較短的路段或樹種關鍵字。", "empty");
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
    showState("請選擇行政區，或輸入路段、樹種開始查詢。");
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
    showState("請選擇行政區，或輸入路段、樹種開始查詢。");
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

initialize();
