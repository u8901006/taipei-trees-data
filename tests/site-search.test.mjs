import assert from "node:assert/strict";
import test from "node:test";

import {
  buildGoogleMapsUrl,
  filterTrees,
  findSpeciesProfile,
  formatMeasurement,
  normalizeQuery,
  paginate,
  protectedValue,
  sanitizePhone,
  validateSpeciesImage,
  validateOfficialUrl,
} from "../site/search.mjs";

const trees = [
  {
    id: "T-1",
    tree_type: "street",
    district: "大安區",
    location: "仁愛路 四段",
    species: "臺灣欒樹",
    diameter: 24.5,
    height: 8,
    updated: "2026-07-30",
    latitude: 25.0392944,
    longitude: 121.5638238,
  },
  {
    id: "T-2",
    tree_type: "park",
    district: "信義區",
    location: "松仁路",
    species: "樟樹",
    diameter: null,
    height: 6.2,
    updated: null,
  },
  {
    id: "T-3",
    tree_type: "street",
    district: "大安區",
    location: "復興南路",
    species: "榕樹",
    diameter: 30,
    height: 10,
    updated: "2026-07-29",
  },
];

test("normalizeQuery applies NFKC, trims and case-folds Latin text", () => {
  assert.equal(normalizeQuery("  ＡBC　仁愛路  "), "abc 仁愛路");
  assert.equal(normalizeQuery(null), "");
});

test("filterTrees combines exact district and partial road or species filters", () => {
  assert.deepEqual(
    filterTrees(trees, { district: "大安區", location: "仁愛", species: "欒樹" }).map(
      (tree) => tree.id,
    ),
    ["T-1"],
  );
  assert.deepEqual(
    filterTrees(trees, { district: "", location: "松仁", species: "" }).map(
      (tree) => tree.id,
    ),
    ["T-2"],
  );
  assert.deepEqual(filterTrees(trees, { district: "", location: "", species: "" }), trees);
});

test("filterTrees supports the public street and park type filter", () => {
  assert.deepEqual(
    filterTrees(trees, { treeType: "park", district: "", location: "", species: "" }).map(
      (tree) => tree.id,
    ),
    ["T-2"],
  );
});

test("filterTrees supports protected trees", () => {
  const protectedTree = { id: "protected:668", tree_type: "protected", district: "大安區" };
  assert.deepEqual(
    filterTrees([...trees, protectedTree], {
      treeType: "protected",
      district: "",
      location: "",
      species: "",
    }),
    [protectedTree],
  );
});

test("buildGoogleMapsUrl requires a finite coordinate pair", () => {
  assert.equal(
    buildGoogleMapsUrl(trees[0]),
    "https://www.google.com/maps/search/?api=1&query=25.0392944%2C121.5638238",
  );
  assert.equal(buildGoogleMapsUrl({ latitude: null, longitude: null }), null);
  assert.equal(buildGoogleMapsUrl({ latitude: 25, longitude: Number.NaN }), null);
});

test("paginate clamps pages and reports stable metadata", () => {
  const records = Array.from({ length: 65 }, (_, index) => ({ id: index + 1 }));
  assert.deepEqual(paginate(records, 2, 30), {
    items: records.slice(30, 60),
    total: 65,
    page: 2,
    pageCount: 3,
  });
  assert.equal(paginate(records, 99, 30).page, 3);
  assert.deepEqual(paginate([], 1, 30), { items: [], total: 0, page: 1, pageCount: 1 });
});

test("formatMeasurement distinguishes missing and numeric values", () => {
  assert.equal(formatMeasurement(null, "公尺"), "未提供");
  assert.equal(formatMeasurement("", "公尺"), "未提供");
  assert.equal(formatMeasurement(8, "公尺"), "8 公尺");
  assert.equal(formatMeasurement(24.5, "公分"), "24.5 公分");
});

test("protected details distinguish pending from official missing values", () => {
  assert.equal(protectedValue({ detail_status: "pending" }, "age_years"), "詳細資料同步中");
  assert.equal(
    protectedValue({ detail_status: "available", age_years: null }, "age_years"),
    "官方未提供",
  );
  assert.equal(protectedValue({ detail_status: "available", age_years: 55 }, "age_years"), 55);
});

test("official URL and phone helpers reject unsafe values", () => {
  assert.equal(validateOfficialUrl("javascript:alert(1)"), null);
  assert.equal(validateOfficialUrl("https://evil.example/image.jpg"), null);
  assert.equal(
    validateOfficialUrl("https://li.taipei/News_Content_VillageLeader.aspx?id=1"),
    "https://li.taipei/News_Content_VillageLeader.aspx?id=1",
  );
  assert.equal(sanitizePhone("0933-902-948"), "0933-902-948");
  assert.equal(sanitizePhone("0933<script>"), null);
});

test("findSpeciesProfile uses normalized exact species names", () => {
  const profiles = [{ species: "榕", tree_count: 10 }, { species: "樟樹", tree_count: 20 }];
  assert.equal(findSpeciesProfile(profiles, " 榕 ").tree_count, 10);
  assert.equal(findSpeciesProfile(profiles, "榕樹"), null);
});

test("species image helper upgrades obsolete Wikimedia thumbnail widths", () => {
  assert.deepEqual(
    validateSpeciesImage({
      status: "available",
      image_url:
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Tree.jpg/900px-Tree.jpg",
      source_page_url: "https://commons.wikimedia.org/wiki/File:Tree.jpg",
      license: "CC BY-SA 4.0",
      artist: null,
      credit: null,
    }),
    {
      status: "available",
      image_url:
        "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Tree.jpg/960px-Tree.jpg",
      source_page_url: "https://commons.wikimedia.org/wiki/File:Tree.jpg",
      license: "CC BY-SA 4.0",
      artist: null,
      credit: null,
    },
  );
});

test("species image helper accepts only Wikimedia photo and source hosts", () => {
  assert.equal(
    validateSpeciesImage({
      status: "available",
      image_url: "https://evil.example/tree.jpg",
      source_page_url: "https://commons.wikimedia.org/wiki/File:Tree.jpg",
    }),
    null,
  );
  assert.equal(
    validateSpeciesImage({
      status: "available",
      image_url: "https://storage.googleapis.com/tbn-filestore/op/occurrence/media/tree.jpg",
      source_page_url: "https://plant.tbn.org.tw/occurrence/verified-tree",
      license: "CC BY",
    })?.license,
    "CC BY",
  );
});
