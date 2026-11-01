const TIFF_PATH_PATTERN = /\.tiff?$/i;

export const viewerImageSource = (item) => {
  const path = item.path || item.filename || "";
  if (TIFF_PATH_PATTERN.test(path)) return item.thumbnail_url || "";
  return item.original_url || "";
};

export const requiresPreviewForViewer = (item) =>
  TIFF_PATH_PATTERN.test(item.path || item.filename || "");
