const TIFF_PATH_PATTERN = /\.tiff?$/i;

export const viewerImageSource = (item) => {
  const path = item.path || item.filename || "";
  if (TIFF_PATH_PATTERN.test(path)) return item.thumbnail_url || "";
  return item.original_url || "";
};

export const requiresPreviewForViewer = (item) =>
  TIFF_PATH_PATTERN.test(item.path || item.filename || "");

export const fitImageDimensions = (
  naturalWidth,
  naturalHeight,
  maxWidth,
  maxHeight,
  rotation = 0,
) => {
  const sideways = Math.abs(rotation) % 180 === 90;
  const width = sideways ? naturalHeight : naturalWidth;
  const height = sideways ? naturalWidth : naturalHeight;
  if (![width, height, maxWidth, maxHeight].every(Number.isFinite) ||
      width <= 0 || height <= 0 || maxWidth <= 0 || maxHeight <= 0) {
    return { width: 0, height: 0 };
  }
  const scale = Math.min(1, maxWidth / width, maxHeight / height);
  return { width: width * scale, height: height * scale };
};
