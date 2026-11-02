import test from "node:test";
import assert from "node:assert/strict";
import { readMockLibraryPage } from "../src/mock-library.js";

test("mock library provides photo records with working local image and download URLs", () => {
  const page = readMockLibraryPage({ offset: 0, limit: 2 });

  assert.equal(page.total, 36);
  assert.equal(page.items.length, 2);
  assert.equal(page.sources.length, 3);
  for (const item of page.items) {
    assert.match(item.original_url, /^data:image\/svg\+xml/);
    assert.equal(item.download_url, item.original_url);
    assert.match(item.filename, /\.svg$/);
  }
});

test("mock library filters by search text and source, then paginates", () => {
  const all = readMockLibraryPage({ sourceId: "SRC-MOCK-02", offset: 0, limit: 100 });
  assert.equal(all.total, 12);
  assert.ok(all.items.every((item) => item.source_id === "SRC-MOCK-02"));

  const filtered = readMockLibraryPage({
    query: "memory-12",
    sourceId: "SRC-MOCK-03",
    offset: 0,
    limit: 10,
  });
  assert.equal(filtered.total, 1);
  assert.equal(filtered.items[0].filename, "memory-12.svg");

  const nextPage = readMockLibraryPage({ offset: 10, limit: 10 });
  assert.equal(nextPage.items[0].filename, "memory-11.svg");
});

test("mock library includes items from any selected disc", () => {
  const page = readMockLibraryPage({
    sourceIds: ["SRC-MOCK-01", "SRC-MOCK-03"],
    offset: 0,
    limit: 100,
  });

  assert.equal(page.total, 24);
  assert.ok(page.items.every((item) => ["SRC-MOCK-01", "SRC-MOCK-03"].includes(item.source_id)));
});

test("mock library reports no videos rather than showing photo records in the video view", () => {
  const page = readMockLibraryPage({ mediaType: "video", offset: 0, limit: 10 });
  assert.equal(page.total, 0);
  assert.deepEqual(page.items, []);
});

test("mock library sorts by file modification date before paginating", () => {
  const newest = readMockLibraryPage({ sort: "newest", offset: 0, limit: 1 });
  const oldest = readMockLibraryPage({ sort: "oldest", offset: 0, limit: 1 });

  assert.equal(newest.items[0].filename, "memory-24.svg");
  assert.equal(oldest.items[0].filename, "memory-25.svg");
});
