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
