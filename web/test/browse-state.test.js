import assert from "node:assert/strict";
import test from "node:test";
import { readBrowseState, writeBrowseState } from "../src/browse-state.js";

class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }
}

test("browse state survives reload with current page and filters", () => {
  const storage = new MemoryStorage();
  const state = {
    offset: 60,
    pageSize: 30,
    query: "summer",
    sourceId: "SRC-0002",
    layout: "list",
  };

  assert.equal(writeBrowseState(state, storage), true);
  assert.deepEqual(readBrowseState(storage), state);
});

test("browse state clamps invalid offsets and ignores unsupported values", () => {
  const storage = new MemoryStorage();
  storage.setItem(
    "family-archive:browse-state:v1",
    JSON.stringify({ offset: 57, pageSize: 25, query: 3, sourceId: null, layout: "other" }),
  );

  assert.deepEqual(readBrowseState(storage), {
    offset: 30,
    pageSize: 30,
    query: "",
    sourceId: "",
    layout: "grid",
  });
});
