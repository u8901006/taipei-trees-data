import assert from "node:assert/strict";
import test from "node:test";

import {
  filterTrees,
  formatMeasurement,
  normalizeQuery,
  paginate,
} from "../site/search.mjs";

const trees = [
  {
    id: "T-1",
    district: "大安區",
    location: "仁愛路 四段",
    species: "臺灣欒樹",
    diameter: 24.5,
    height: 8,
    updated: "2026-07-30",
  },
  {
    id: "T-2",
    district: "信義區",
    location: "松仁路",
    species: "樟樹",
    diameter: null,
    height: 6.2,
    updated: null,
  },
  {
    id: "T-3",
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
