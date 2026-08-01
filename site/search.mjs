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
