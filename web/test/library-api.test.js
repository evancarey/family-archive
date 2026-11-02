import assert from "node:assert/strict";
import test from "node:test";
import { loadPresentationConfig } from "../src/config.js";

const values = new Map();
globalThis.sessionStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, String(value)),
  removeItem: (key) => values.delete(key),
};
const location = { search: "", pathname: "/" };
globalThis.window = {
  location,
  history: { replaceState: () => { location.search = ""; } },
};
globalThis.document = { title: "Family Archive" };

const config = {
  apiUrl: "https://api.example.test",
  cognitoDomain: "https://login.example.test",
  clientId: "public-client",
  redirectUri: "https://family.example.test/",
};
globalThis.fetch = async (url) => {
  assert.equal(url, "/config.json");
  return { ok: true, json: async () => config };
};
await loadPresentationConfig();

test("refreshes the access token before API requests and reports renewal failure", async () => {
  values.set(
    "family-archive.oauth-session",
    JSON.stringify({
      accessToken: "initial-token",
      refreshToken: "saved-refresh-token",
      expiresAt: Date.now() + 3_600_000,
    }),
  );
  const auth = await import("../src/auth.js");
  const { checkLibraryApi, SignInRequiredError } = await import("../src/library-api.js");
  assert.equal(await auth.finishSignIn(), true);

  const originalNow = Date.now;
  const startedAt = originalNow();
  let rejectRefresh = false;
  Date.now = () => startedAt + 3_570_000;
  globalThis.fetch = async (url, options) => {
    if (url === "https://login.example.test/oauth2/token") {
      assert.match(String(options.body), /grant_type=refresh_token/);
      return rejectRefresh
        ? { ok: false, status: 400 }
        : {
            ok: true,
            json: async () => ({ access_token: "renewed-token", expires_in: 3600 }),
          };
    }
    assert.equal(url, "https://api.example.test/api/health");
    assert.equal(options.headers.authorization, "Bearer renewed-token");
    return { ok: true, json: async () => ({ status: "ready" }) };
  };

  try {
    assert.equal(await checkLibraryApi(), true);
    rejectRefresh = true;
    Date.now = () => startedAt + 7_170_000;
    await assert.rejects(checkLibraryApi(), (error) => {
      assert.ok(error instanceof SignInRequiredError);
      assert.match(error.message, /Sign in again to continue/);
      return true;
    });
  } finally {
    Date.now = originalNow;
  }
});

test("renders no more photos than the selected page size if the API over-returns", async () => {
  values.set(
    "family-archive.oauth-session",
    JSON.stringify({
      accessToken: "page-token",
      refreshToken: null,
      expiresAt: Date.now() + 3_600_000,
    }),
  );
  const auth = await import("../src/auth.js");
  const { fetchPhotos } = await import("../src/library-api.js");
  assert.equal(await auth.finishSignIn(), true);

  globalThis.fetch = async (url, options) => {
    if (url === "/config.json") return { ok: true, json: async () => config };
    assert.equal(new URL(url).searchParams.get("limit"), "10");
    assert.equal(options.headers.authorization, "Bearer page-token");
    return {
      ok: true,
      status: 200,
      json: async () => ({
        total: 30,
        items: Array.from({ length: 30 }, (_, index) => ({ filename: `photo-${index}.jpg` })),
      }),
    };
  };

  const result = await fetchPhotos({ query: "", sourceId: "", offset: 0, limit: 10 });

  assert.equal(result.payload.total, 30);
  assert.equal(result.payload.items.length, 10);
});

test("requests a paginated video library page", async () => {
  values.set(
    "family-archive.oauth-session",
    JSON.stringify({
      accessToken: "video-token",
      refreshToken: null,
      expiresAt: Date.now() + 3_600_000,
    }),
  );
  const auth = await import("../src/auth.js");
  const { fetchLibraryMedia } = await import("../src/library-api.js");
  assert.equal(await auth.finishSignIn(), true);

  globalThis.fetch = async (url, options) => {
    const requestUrl = new URL(url);
    assert.equal(requestUrl.searchParams.get("media"), "video");
    assert.equal(requestUrl.searchParams.get("limit"), "10");
    assert.equal(requestUrl.searchParams.get("offset"), "20");
    assert.equal(requestUrl.searchParams.get("sort"), "newest");
    assert.equal(requestUrl.searchParams.get("source"), "SRC-0001,SRC-0002");
    assert.equal(options.headers.authorization, "Bearer video-token");
    return {
      ok: true,
      status: 200,
      json: async () => ({ total: 1, items: [{ filename: "home-movie.mov" }] }),
    };
  };

  const result = await fetchLibraryMedia({
    query: "movie",
    sourceIds: ["SRC-0001", "SRC-0002"],
    offset: 20,
    limit: 10,
    mediaType: "video",
    sort: "newest",
  });

  assert.equal(result.payload.items[0].filename, "home-movie.mov");
});

test("saves shared file tags with an optimistic version", async () => {
  values.set(
    "family-archive.oauth-session",
    JSON.stringify({
      accessToken: "tag-token",
      refreshToken: null,
      expiresAt: Date.now() + 3_600_000,
    }),
  );
  const auth = await import("../src/auth.js");
  const { updateFileTags } = await import("../src/library-api.js");
  assert.equal(await auth.finishSignIn(), true);

  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://api.example.test/api/tags");
    assert.equal(options.method, "PUT");
    assert.equal(options.headers.authorization, "Bearer tag-token");
    assert.deepEqual(JSON.parse(options.body), {
      sha256: "a".repeat(64),
      tags: ["family"],
      expected_version: 2,
    });
    return {
      ok: true,
      status: 200,
      json: async () => ({ sha256: "a".repeat(64), tags: ["family"], version: 3 }),
    };
  };

  const result = await updateFileTags({
    sha256: "a".repeat(64),
    tags: ["family"],
    expectedVersion: 2,
  });
  assert.equal(result.version, 3);
});
