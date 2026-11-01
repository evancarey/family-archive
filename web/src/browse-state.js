const STORAGE_KEY = "family-archive:browse-state:v1";
export const PAGE_SIZE_OPTIONS = Object.freeze(
  Array.from({ length: 10 }, (_, index) => (index + 1) * 10),
);

const defaults = () => ({
  offset: 0,
  pageSize: 30,
  query: "",
  sourceId: "",
  layout: "grid",
});

export const readBrowseState = (storage = globalThis.sessionStorage) => {
  try {
    const saved = JSON.parse(storage.getItem(STORAGE_KEY) || "null");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return defaults();
    const pageSize = PAGE_SIZE_OPTIONS.includes(saved.pageSize) ? saved.pageSize : 30;
    const requestedOffset = Number.isSafeInteger(saved.offset) && saved.offset >= 0
      ? saved.offset
      : 0;
    return {
      offset: Math.floor(requestedOffset / pageSize) * pageSize,
      pageSize,
      query: typeof saved.query === "string" ? saved.query : "",
      sourceId: typeof saved.sourceId === "string" ? saved.sourceId : "",
      layout: saved.layout === "list" ? "list" : "grid",
    };
  } catch {
    return defaults();
  }
};

export const writeBrowseState = (state, storage = globalThis.sessionStorage) => {
  try {
    const value = {
      offset: Number.isSafeInteger(state.offset) && state.offset >= 0 ? state.offset : 0,
      pageSize: PAGE_SIZE_OPTIONS.includes(state.pageSize) ? state.pageSize : 30,
      query: typeof state.query === "string" ? state.query : "",
      sourceId: typeof state.sourceId === "string" ? state.sourceId : "",
      layout: state.layout === "list" ? "list" : "grid",
    };
    value.offset = Math.floor(value.offset / value.pageSize) * value.pageSize;
    storage.setItem(STORAGE_KEY, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
};
