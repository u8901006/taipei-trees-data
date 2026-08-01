export function normalizeQuery(value) {
  if (value === null || value === undefined) return "";
  return String(value).normalize("NFKC").trim().replace(/\s+/gu, " ").toLocaleLowerCase("zh-TW");
}

export function filterTrees(records, filters) {
  const treeType = normalizeQuery(filters.treeType);
  const district = normalizeQuery(filters.district);
  const location = normalizeQuery(filters.location);
  const species = normalizeQuery(filters.species);
  return records.filter((record) => {
    if (treeType && normalizeQuery(record.tree_type) !== treeType) return false;
    if (district && normalizeQuery(record.district) !== district) return false;
    if (location && !normalizeQuery(record.location).includes(location)) return false;
    if (species && !normalizeQuery(record.species).includes(species)) return false;
    return true;
  });
}

export function buildGoogleMapsUrl(tree) {
  const latitude = Number(tree?.latitude);
  const longitude = Number(tree?.longitude);
  if (
    tree?.latitude === null ||
    tree?.latitude === undefined ||
    tree?.longitude === null ||
    tree?.longitude === undefined ||
    !Number.isFinite(latitude) ||
    !Number.isFinite(longitude)
  ) {
    return null;
  }
  const query = encodeURIComponent(`${latitude},${longitude}`);
  return `https://www.google.com/maps/search/?api=1&query=${query}`;
}

export function paginate(records, requestedPage, requestedPageSize) {
  const pageSize = Math.max(1, Number.parseInt(requestedPageSize, 10) || 30);
  const pageCount = Math.max(1, Math.ceil(records.length / pageSize));
  const page = Math.min(pageCount, Math.max(1, Number.parseInt(requestedPage, 10) || 1));
  const start = (page - 1) * pageSize;
  return {
    items: records.slice(start, start + pageSize),
    total: records.length,
    page,
    pageCount,
  };
}

export function formatMeasurement(value, unit) {
  if (value === null || value === undefined || value === "") return "未提供";
  return `${value} ${unit}`;
}

export function protectedValue(tree, field) {
  if (tree?.detail_status === "pending") return "詳細資料同步中";
  const value = tree?.[field];
  return value === null || value === undefined || value === "" ? "官方未提供" : value;
}

export function validateOfficialUrl(value) {
  try {
    const url = new URL(value);
    const hostname = url.hostname.toLocaleLowerCase("en-US");
    const officialHost =
      hostname === "gov.taipei" ||
      hostname.endsWith(".gov.taipei") ||
      hostname === "li.taipei" ||
      hostname === "tai2.ntu.edu.tw" ||
      hostname === "www.tbn.org.tw";
    if (url.protocol !== "https:" || !officialHost || url.username || url.password || url.hash) {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

export function sanitizePhone(value) {
  if (typeof value !== "string") return null;
  const cleaned = value.trim();
  if (!/^[+0-9-]+$/u.test(cleaned) || cleaned.replace(/\D/gu, "").length < 8) return null;
  return cleaned;
}

export function findSpeciesProfile(profiles, species) {
  const query = normalizeQuery(species);
  if (!query || !Array.isArray(profiles)) return null;
  return profiles.find((profile) => normalizeQuery(profile?.species) === query) || null;
}
