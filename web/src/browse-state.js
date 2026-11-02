const STORAGE_KEY = "family-archive:browse-state:v1";
export const PAGE_SIZE_OPTIONS = Object.freeze(
  Array.from({ length: 10 }, (_, index) => (index + 1) * 10),
);

const defaults = () => ({
  offset: 0,
  pageSize: 30,
  query: "",
  sourceIds: [],
  mediaType: "photo",
  layout: "grid",
  sort: "archive",
});

export const readBrowseState = (storage = globalThis.localStorage) => {
  try {
    let serialized = storage.getItem(STORAGE_KEY);
    if (serialized === null && storage === globalThis.localStorage) {
      serialized = globalThis.sessionStorage?.getItem(STORAGE_KEY) ?? null;
      if (serialized !== null) storage.setItem(STORAGE_KEY, serialized);
    }
    const saved = JSON.parse(serialized || "null");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return defaults();
    const pageSize = PAGE_SIZE_OPTIONS.includes(saved.pageSize) ? saved.pageSize : 30;
    const requestedOffset = Number.isSafeInteger(saved.offset) && saved.offset >= 0
      ? saved.offset
      : 0;
    return {
      offset: requestedOffset,
      pageSize,
      query: typeof saved.query === "string" ? saved.query : "",
      sourceIds: Array.isArray(saved.sourceIds)
        ? [...new Set(saved.sourceIds.filter((sourceId) => typeof sourceId === "string" && sourceId))]
        : typeof saved.sourceId === "string" && saved.sourceId
        ? [saved.sourceId]
        : [],
      mediaType: saved.mediaType === "video" ? "video" : "photo",
      layout: saved.layout === "list" ? "list" : "grid",
      sort: ["archive", "newest", "oldest"].includes(saved.sort) ? saved.sort : "archive",
    };
  } catch {
    return defaults();
  }
};

export const writeBrowseState = (state, storage = globalThis.localStorage) => {
  try {
    const value = {
      offset: Number.isSafeInteger(state.offset) && state.offset >= 0 ? state.offset : 0,
      pageSize: PAGE_SIZE_OPTIONS.includes(state.pageSize) ? state.pageSize : 30,
      query: typeof state.query === "string" ? state.query : "",
      sourceIds: Array.isArray(state.sourceIds)
        ? [...new Set(state.sourceIds.filter((sourceId) => typeof sourceId === "string" && sourceId))]
        : typeof state.sourceId === "string" && state.sourceId
        ? [state.sourceId]
        : [],
      mediaType: state.mediaType === "video" ? "video" : "photo",
      layout: state.layout === "list" ? "list" : "grid",
      sort: ["archive", "newest", "oldest"].includes(state.sort) ? state.sort : "archive",
    };
    storage.setItem(STORAGE_KEY, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
};
