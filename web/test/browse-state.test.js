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

test("browse preferences default to persistent local storage", () => {
  const storage = new MemoryStorage();
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: storage,
  });

  try {
    const expected = {
      offset: 20,
      pageSize: 10,
      query: "portrait",
      sourceId: "SRC-0001",
      layout: "list",
    };
    assert.equal(writeBrowseState(expected), true);
    assert.deepEqual(readBrowseState(), expected);
  } finally {
    if (descriptor) Object.defineProperty(globalThis, "localStorage", descriptor);
    else delete globalThis.localStorage;
  }
});

test("migrates a tab-scoped browse preference into local storage", () => {
  const persistent = new MemoryStorage();
  const legacy = new MemoryStorage();
  const previousLocal = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const previousSession = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: persistent,
  });
  Object.defineProperty(globalThis, "sessionStorage", {
    configurable: true,
    value: legacy,
  });
  legacy.setItem(
    "family-archive:browse-state:v1",
    JSON.stringify({ offset: 0, pageSize: 10, query: "", sourceId: "", layout: "grid" }),
  );

  try {
    assert.equal(readBrowseState().pageSize, 10);
    assert.equal(JSON.parse(persistent.getItem("family-archive:browse-state:v1")).pageSize, 10);
  } finally {
    if (previousLocal) Object.defineProperty(globalThis, "localStorage", previousLocal);
    else delete globalThis.localStorage;
    if (previousSession) Object.defineProperty(globalThis, "sessionStorage", previousSession);
    else delete globalThis.sessionStorage;
  }
});
