import test from "node:test";
import assert from "node:assert/strict";
import {
  archiveEntryKey,
  imageRotationFor,
  imageRotationKey,
  rotateAngle,
} from "../src/rotation.js";

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

test("identical file hashes share a rotation while archive entries remain distinct", () => {
  const firstCopy = { source_id: "SRC-0001", path: "A/photo.jpg", sha256: "same-hash" };
  const secondCopy = { source_id: "SRC-0002", path: "B/photo.jpg", sha256: "same-hash" };

  assert.equal(imageRotationKey(firstCopy), imageRotationKey(secondCopy));
  assert.notEqual(archiveEntryKey(firstCopy), archiveEntryKey(secondCopy));
});

test("existing per-entry rotations remain visible until a shared rotation is saved", () => {
  const firstCopy = { source_id: "SRC-0001", path: "A/photo.jpg", sha256: "same-hash" };
  const secondCopy = { source_id: "SRC-0002", path: "B/photo.jpg", sha256: "same-hash" };
  const rotations = { [archiveEntryKey(firstCopy)]: 90 };

  assert.equal(imageRotationFor(rotations, firstCopy), 90);
  assert.equal(imageRotationFor(rotations, secondCopy), 0);
  rotations[imageRotationKey(firstCopy)] = 90;
  assert.equal(imageRotationFor(rotations, secondCopy), 90);
});
