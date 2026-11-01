import test from "node:test";
import assert from "node:assert/strict";
import { rotateAngle } from "../src/rotation.js";

test("first counterclockwise rotation turns an unrotated image left", () => {
  assert.equal(rotateAngle(0, -90), 270);
});

test("counterclockwise rotation keeps turning left on subsequent clicks", () => {
  assert.equal(rotateAngle(270, -90), 180);
  assert.equal(rotateAngle(180, -90), 90);
  assert.equal(rotateAngle(90, -90), 0);
});

test("clockwise rotation turns an unrotated image right", () => {
  assert.equal(rotateAngle(0, 90), 90);
});
