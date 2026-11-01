import test from "node:test";
import assert from "node:assert/strict";
import {
  fitImageDimensions,
  requiresPreviewForViewer,
  viewerImageSource,
} from "../src/image-display.js";

test("TIFF viewer uses the browser-compatible JPEG preview", () => {
  const item = {
    path: "Family/scan.TIFF",
    original_url: "https://example.test/preserved.tiff",
    thumbnail_url: "https://example.test/preview.jpg",
  };

  assert.equal(requiresPreviewForViewer(item), true);
  assert.equal(viewerImageSource(item), item.thumbnail_url);
});

test("TIFF viewer reports no image source when a preview is absent", () => {
  assert.equal(viewerImageSource({ filename: "scan.tif", original_url: "original" }), "");
});

test("browser-supported formats continue to use the preserved original", () => {
  const item = {
    path: "Family/photo.jpg",
    original_url: "https://example.test/original.jpg",
    thumbnail_url: "https://example.test/preview.jpg",
  };

  assert.equal(requiresPreviewForViewer(item), false);
  assert.equal(viewerImageSource(item), item.original_url);
});

test("portrait originals keep their aspect ratio within the viewer bounds", () => {
  assert.deepEqual(fitImageDimensions(1536, 2048, 1200, 900), {
    width: 675,
    height: 900,
  });
});

test("quarter-turn rotation fits using the rotated aspect ratio", () => {
  assert.deepEqual(fitImageDimensions(1536, 2048, 1200, 900, 90), {
    width: 1200,
    height: 900,
  });
});
