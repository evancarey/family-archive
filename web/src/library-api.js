import { getPresentationConfig } from "./config.js";
import { getValidAccessToken } from "./auth.js";

export class SignInRequiredError extends Error {
  constructor() {
    super("Your sign-in session expired and could not be renewed. Sign in again to continue.");
    this.name = "SignInRequiredError";
  }
}

const endpoint = (base, path) => `${base.replace(/\/+$/, "")}${path}`;

const authenticatedFetch = async (path) => {
  const token = await getValidAccessToken();
  if (!token) throw new SignInRequiredError();
  const config = getPresentationConfig();
  return fetch(endpoint(config.apiUrl, path), {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });
};

export const checkLibraryApi = async () => {
  const response = await authenticatedFetch("/api/health");
  if (!response.ok) throw new Error("Your sign-in could not be confirmed by the library service.");
  const result = await response.json();
  return result.status === "ready";
};

export const fetchLibraryMedia = async ({
  query,
  sourceId = "",
  sourceIds = sourceId ? [sourceId] : [],
  offset,
  limit = 30,
  mediaType = "photo",
  sort = "archive",
}) => {
  if (!["photo", "video"].includes(mediaType)) {
    throw new TypeError("The library media type must be photo or video.");
  }
  const config = getPresentationConfig();
  const url = new URL(endpoint(config.apiUrl, "/api/library"));
  url.search = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
    media: mediaType,
    sort,
    q: query,
    source: sourceIds.join(","),
  });
  const token = await getValidAccessToken();
  if (!token) throw new SignInRequiredError();
  const response = await fetch(url, {
    headers: { authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  const payload = await response.json();
  if (!response.ok && response.status !== 404) {
    throw new Error(payload.error || "The photographs could not be loaded.");
  }
  const page = {
    ...payload,
    items: Array.isArray(payload.items) ? payload.items.slice(0, limit) : [],
  };
  return { status: response.status, payload: page };
};

export const fetchPhotos = (options) =>
  fetchLibraryMedia({ ...options, mediaType: "photo" });

export const updateFileTags = async ({ sha256, tags, expectedVersion }) => {
  const token = await getValidAccessToken();
  if (!token) throw new SignInRequiredError();
  const config = getPresentationConfig();
  const response = await fetch(endpoint(config.apiUrl, "/api/tags"), {
    method: "PUT",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ sha256, tags, expected_version: expectedVersion }),
    cache: "no-store",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Tags could not be saved.");
  return payload;
};
