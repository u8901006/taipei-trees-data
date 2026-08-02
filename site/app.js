import {
  buildGoogleMapsUrl,
  filterTrees,
  findSpeciesProfile,
  formatMeasurement,
  normalizeQuery,
  paginate,
  protectedValue,
  sanitizePhone,
  validateOfficialUrl,
  validateSpeciesImage,
} from "./search.mjs";

const PAGE_SIZE = 30;
const number = new Intl.NumberFormat("zh-TW");
const cache = new Map();
let manifest = null;
let schedules = [];
let scheduleCandidateCounts = new Map();
let schedulesById = new Map();
let matchedTrees = [];
let speciesProfiles = [];
let speciesImages = new Map();
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
  speciesDialog: document.querySelector("#species-dialog"),
  speciesDialogContent: document.querySelector("#species-dialog-content"),
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
  return validateOfficialUrl(value);
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
  if (schedule.requester_type === "village_chief_recommendation") {
    appendDetail(details, "所屬里", schedule.village || "里別待確認");
    appendDetail(details, "里別判定", schedule.village_match_method || "unresolved");
  }

  const officialUrl = sourceUrl(schedule.source_url);
  const source = document.createElement(officialUrl ? "a" : "span");
  source.className = "official-source";
  source.textContent = officialUrl ? "查看官方行程來源 ↗" : "官方來源連結未通過驗證";
  if (officialUrl) {
    source.href = officialUrl;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
  }
  card.append(heading, details);
  if (schedule.requester_type === "village_chief_recommendation") {
    const leader = document.createElement("aside");
    leader.className = "leader-card";
    const leaderHeading = document.createElement("h4");
    leaderHeading.textContent = schedule.village_leader_name
      ? `${schedule.village_leader_name} 里長`
      : "現任里長資料待確認";
    leader.append(leaderHeading);
    const phone = sanitizePhone(schedule.village_leader_mobile);
    if (phone) {
      const phoneLink = document.createElement("a");
      phoneLink.href = `tel:${phone}`;
      phoneLink.textContent = `撥打公開電話 ${phone}`;
      leader.append(phoneLink);
    }
    const profileUrl = sourceUrl(schedule.village_leader_profile_url);
    if (profileUrl) {
      const profile = document.createElement("a");
      profile.href = profileUrl;
      profile.target = "_blank";
      profile.rel = "noopener noreferrer";
      profile.textContent = "查看里長簡介 ↗";
      leader.append(profile);
    }
    const villageSourceUrl = sourceUrl(schedule.village_match_source_url);
    if (villageSourceUrl) {
      const evidence = document.createElement("a");
      evidence.href = villageSourceUrl;
      evidence.target = "_blank";
      evidence.rel = "noopener noreferrer";
      evidence.textContent = "查看里別判定來源 ↗";
      leader.append(evidence);
    }
    card.append(leader);
  }
  card.append(source);
  return card;
}

function appendLink(container, label, value, className = "official-source") {
  const url = sourceUrl(value);
  if (!url) return;
  const link = document.createElement("a");
  link.className = className;
  link.href = url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = label;
  container.append(link);
}

function createProtectedDetails(tree) {
  if (tree.tree_type !== "protected") return null;
  const details = document.createElement("details");
  details.className = "protected-details";
  const summary = document.createElement("summary");
  summary.textContent = "老樹檔案與故事";
  const content = document.createElement("div");
  content.className = "protected-details-content";
  const badge = document.createElement("span");
  badge.className = "protected-badge";
  badge.textContent = "受保護樹木／臺北老樹";
  const facts = document.createElement("dl");
  facts.className = "protected-facts";
  const age = protectedValue(tree, "age_years");
  appendDetail(facts, "官方登錄樹齡", typeof age === "number" ? `${age} 年` : age);
  appendDetail(facts, "推估植栽年", String(protectedValue(tree, "born_year")));
  appendDetail(facts, "里別", String(protectedValue(tree, "village")));
  appendDetail(facts, "學名", tree.scientific_name || "官方未提供");
  appendDetail(facts, "英文名", tree.english_name || "官方未提供");
  appendDetail(facts, "管理單位", tree.management_unit || "官方未提供");
  appendDetail(facts, "檔案照片", tree.detail_status === "pending" ? "詳細資料同步中" : `${tree.photo_count || 0} 張`);
  content.append(badge, facts);
  if (tree.story || tree.environment_description) {
    const story = document.createElement("p");
    story.className = "protected-story";
    story.textContent = tree.story || tree.environment_description;
    content.append(story);
  } else {
    const missing = document.createElement("p");
    missing.className = "protected-story";
    missing.textContent = protectedValue(tree, "story");
    content.append(missing);
  }
  const links = document.createElement("div");
  links.className = "protected-links";
  appendLink(links, "查看官方檔案照片 ↗", tree.photo_url);
  appendLink(links, "查看官方詳細位置與故事 ↗", tree.official_detail_url);
  content.append(links);
  details.append(summary, content);
  return details;
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

  const protectedDetails = createProtectedDetails(tree);
  if (protectedDetails) actions.append(protectedDetails);

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

function openSpeciesProfile(species) {
  const profile = findSpeciesProfile(speciesProfiles, species);
  const content = elements.speciesDialogContent;
  content.replaceChildren();
  const title = document.createElement("h2");
  title.id = "species-dialog-title";
  title.textContent = `${species || "未命名樹種"}介紹`;
  content.append(title);
  if (!profile) {
    const unavailable = document.createElement("p");
    unavailable.textContent = "目前沒有可驗證的樹種統計資料。";
    content.append(unavailable);
  } else {
    const imageRecord = speciesImages.get(profile.species);
    const photo = document.createElement("figure");
    photo.className = "species-photo";
    const verifiedImage = validateSpeciesImage(imageRecord);
    if (verifiedImage) {
      const sourceLink = document.createElement("a");
      sourceLink.href = verifiedImage.source_page_url;
      sourceLink.target = "_blank";
      sourceLink.rel = "noopener noreferrer";
      sourceLink.setAttribute("aria-label", `查看${profile.species}照片來源與授權`);
      const image = document.createElement("img");
      image.src = verifiedImage.image_url;
      image.alt = `${profile.species}樹種照片`;
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => {
        photo.classList.add("species-photo--pending");
        photo.setAttribute("role", "img");
        photo.setAttribute("aria-label", `${profile.species}照片暫時無法載入`);
        const unavailable = document.createElement("span");
        unavailable.textContent = "照片暫時無法載入，請查看來源原頁";
        photo.replaceChildren(unavailable);
      });
      sourceLink.append(image);
      const caption = document.createElement("figcaption");
      const sourceHost = new URL(verifiedImage.source_page_url).hostname;
      const sourceName = sourceHost === "plant.tbn.org.tw" ? "農業部 TBN" : sourceHost === "zh.wikipedia.org" ? "中文維基百科" : "Wikimedia Commons";
      caption.textContent = [
        `照片：${sourceName}`,
        verifiedImage.artist ? `作者 ${verifiedImage.artist}` : null,
        verifiedImage.license || "授權資訊請見來源頁",
      ]
        .filter(Boolean)
        .join("｜");
      photo.append(sourceLink, caption);
    } else {
      photo.classList.add("species-photo--pending");
      photo.setAttribute("role", "img");
      photo.setAttribute("aria-label", `${profile.species}照片尚待核實`);
      const unavailable = document.createElement("span");
      unavailable.textContent = imageRecord?.status === "unavailable" ? "尚無可核實照片" : "照片同步中";
      photo.append(unavailable);
    }
    const names = document.createElement("p");
    names.className = "species-names";
    names.textContent = [profile.scientific_name, profile.english_name].filter(Boolean).join("｜") || "官方未提供學名或英文名";
    const facts = document.createElement("dl");
    facts.className = "species-facts";
    appendDetail(facts, "臺北市收錄", `${number.format(profile.tree_count)} 棵`);
    appendDetail(facts, "受保護樹木", `${number.format(profile.protected_tree_count)} 棵`);
    appendDetail(facts, "平均胸徑", formatMeasurement(profile.average_diameter_cm, "cm"));
    appendDetail(facts, "平均樹高", formatMeasurement(profile.average_height_m, "m"));
    const districts = document.createElement("p");
    districts.textContent = `行政區分布：${Object.entries(profile.district_counts || {}).map(([name, count]) => `${name} ${number.format(count)}`).join("、") || "官方未提供"}`;
    const locations = document.createElement("p");
    locations.textContent = `常見地點：${(profile.common_locations || []).map((item) => `${item.name}（${number.format(item.count)}）`).join("、") || "官方未提供"}`;
    const links = document.createElement("div");
    links.className = "species-links";
    for (const item of profile.authoritative_links || []) appendLink(links, `${item.label} ↗`, item.url);
    content.append(photo, names, facts, districts, locations, links);
  }
  if (typeof elements.speciesDialog.showModal === "function") elements.speciesDialog.showModal();
  else elements.speciesDialog.setAttribute("open", "");
}

function createSpeciesCell(tree) {
  const cell = document.createElement("td");
  cell.dataset.label = "樹種";
  const button = document.createElement("button");
  button.className = "species-link";
  button.type = "button";
  button.textContent = tree.species || "未提供";
  button.disabled = !tree.species;
  button.addEventListener("click", () => openSpeciesProfile(tree.species));
  cell.append(button);
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
    row.append(createSpeciesCell(tree));
    addCell(row, "胸徑", formatMeasurement(tree.diameter, "cm"));
    addCell(row, "樹高", formatMeasurement(tree.height, "m"));
    const age = tree.tree_type === "protected" ? protectedValue(tree, "age_years") : "官方未提供";
    addCell(row, "樹齡", typeof age === "number" ? `${age} 年` : age);
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
  const leaders = schedules.filter((schedule) => schedule.village_leader_name).length;
  document.querySelector("#leader-data-status").textContent = leaders
    ? `${number.format(leaders)} 筆已連結`
    : "目前無可連結案件";
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
    document.querySelector("#protected-count").textContent = number.format(
      manifest.type_counts?.protected || 0,
    );
    const coverage = manifest.protected_detail_coverage || {};
    document.querySelector("#protected-detail-coverage").textContent = `${number.format(coverage.available || 0)} / ${number.format(coverage.total || 0)}`;
    document.querySelector("#species-profile-count").textContent = number.format(
      manifest.species_profile_count || 0,
    );
    const speciesDocument = await fetchJson(`./data/${manifest.species_profile_file}`);
    speciesProfiles = Array.isArray(speciesDocument.profiles) ? speciesDocument.profiles : [];
    const imageDocument = await fetchJson("./data/species_images.json").catch(() => ({ records: {} }));
    speciesImages = new Map(Object.entries(imageDocument.records || {}));
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
