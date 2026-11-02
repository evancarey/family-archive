const SOURCES = Object.freeze([
  { source_id: "SRC-MOCK-01", volume_label: "Summer visits" },
  { source_id: "SRC-MOCK-02", volume_label: "At home" },
  { source_id: "SRC-MOCK-03", volume_label: "Old family scans" },
]);

const COLORS = [
  ["#d9b578", "#587a65", "#f3e6ca"],
  ["#8cb6bc", "#416b69", "#f4e4c5"],
  ["#d89d79", "#71647d", "#f2d7a4"],
  ["#b9c28a", "#5b7050", "#f5d6a0"],
  ["#bd9fb5", "#6e6e8c", "#f1dfbe"],
  ["#95a9c3", "#4d6f81", "#f4ddb3"],
];

const svgUrl = (number, colors) => {
  const [sky, ground, sun] = colors;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">
    <defs><linearGradient id="sky" x2="0" y2="1"><stop stop-color="${sky}"/><stop offset="1" stop-color="#f3e8d2"/></linearGradient></defs>
    <rect width="800" height="600" fill="url(#sky)"/><circle cx="${540 + (number % 5) * 28}" cy="160" r="58" fill="${sun}"/>
    <path d="M0 390 Q180 250 350 395 T800 350 V600 H0Z" fill="${ground}"/>
    <path d="M0 470 Q210 330 420 485 T800 430 V600 H0Z" fill="#344f48" opacity=".48"/>
    <text x="40" y="548" fill="#fffaf0" font-family="Georgia,serif" font-size="30">Memory ${String(number).padStart(2, "0")}</text>
  </svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
};

const MOCK_ITEMS = Object.freeze(
  Array.from({ length: 36 }, (_, index) => {
    const number = index + 1;
    const source = SOURCES[index % SOURCES.length];
    const filename = `memory-${String(number).padStart(2, "0")}.svg`;
    const path = `Photos/Family/${filename}`;
    const imageUrl = svgUrl(number, COLORS[index % COLORS.length]);
    return Object.freeze({
      ...source,
      filename,
      path,
      sha256: number.toString(16).padStart(64, "0"),
      media_type: "photo",
      mtime_ns: Date.UTC(2000 + (number % 25), number % 12, (number % 27) + 1) * 1_000_000,
      thumbnail_url: imageUrl,
      original_url: imageUrl,
      download_url: imageUrl,
    });
  }),
);

export const mockLibraryEnabled = () =>
  import.meta.env?.DEV === true &&
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("mockLibrary") === "1";

export const readMockLibraryPage = ({
  query = "",
  sourceId = "",
  sourceIds = sourceId ? [sourceId] : [],
  mediaType = "photo",
  sort = "archive",
  offset = 0,
  limit = 30,
}) => {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filtered = MOCK_ITEMS.filter((item) => {
    if ((item.media_type || "photo") !== mediaType) return false;
    if (sourceIds.length && !sourceIds.includes(item.source_id)) return false;
    if (!normalizedQuery) return true;
    return `${item.filename} ${item.path} ${item.volume_label} ${item.source_id}`
      .toLocaleLowerCase()
      .includes(normalizedQuery);
  });
  if (sort === "newest") filtered.sort((a, b) => b.mtime_ns - a.mtime_ns);
  if (sort === "oldest") filtered.sort((a, b) => a.mtime_ns - b.mtime_ns);
  return {
    sources: SOURCES,
    total: filtered.length,
    items: filtered.slice(offset, offset + limit),
  };
};
