import assert from "node:assert/strict";
import test from "node:test";
import { pageTokens, placeholderCount } from "../src/pagination.js";

test("shows every page when the result set is short", () => {
  assert.deepEqual(pageTokens(2, 5), [1, 2, 3, 4, 5]);
});

test("shows nearby pages and jumps at the beginning", () => {
  assert.deepEqual(pageTokens(2, 20), [1, 2, 3, 4, 5, "…", 20]);
});

test("shows nearby pages around the current page", () => {
  assert.deepEqual(pageTokens(10, 20), [1, "…", 9, 10, 11, "…", 20]);
});

test("shows nearby pages and jumps at the end", () => {
  assert.deepEqual(pageTokens(19, 20), [1, "…", 16, 17, 18, 19, 20]);
});

test("fills the current page with placeholders, bounded by the remaining results", () => {
  assert.equal(placeholderCount(103, 30, 30), 30);
  assert.equal(placeholderCount(103, 90, 30), 13);
  assert.equal(placeholderCount(103, 120, 30), 0);
});
