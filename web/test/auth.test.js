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
  history: {
    replaceState: () => {
      location.search = "";
    },
  },
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

const freshAuthModule = () => import(`../src/auth.js?test=${crypto.randomUUID()}`);

test("restores an unexpired sign-in after a page reload", async () => {
  const auth = await freshAuthModule();
  values.set(
    "family-archive.oauth-transaction",
    JSON.stringify({ verifier: "pkce-verifier", state: "expected-state" }),
  );
  location.search = "?code=authorization-code&state=expected-state";
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://login.example.test/oauth2/token");
    assert.match(String(options.body), /grant_type=authorization_code/);
    return {
      ok: true,
      json: async () => ({
        access_token: "first-access-token",
        refresh_token: "long-lived-refresh-token",
        expires_in: 3600,
      }),
    };
  };

  assert.equal(await auth.finishSignIn(), true);
  assert.equal(auth.currentAccessToken(), "first-access-token");
  assert.ok(values.has("family-archive.oauth-session"));

  const reloadedAuth = await freshAuthModule();
  globalThis.fetch = async () => {
    throw new Error("A valid session should not require a network token exchange");
  };
  assert.equal(await reloadedAuth.finishSignIn(), true);
  assert.equal(reloadedAuth.currentAccessToken(), "first-access-token");
});

test("renews an expired access token from the saved refresh token", async () => {
  values.set(
    "family-archive.oauth-session",
    JSON.stringify({
      accessToken: "expired-access-token",
      refreshToken: "saved-refresh-token",
      expiresAt: Date.now() - 1000,
    }),
  );
  location.search = "";
  const auth = await freshAuthModule();
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://login.example.test/oauth2/token");
    assert.match(String(options.body), /grant_type=refresh_token/);
    assert.match(String(options.body), /refresh_token=saved-refresh-token/);
    return {
      ok: true,
      json: async () => ({ access_token: "renewed-access-token", expires_in: 3600 }),
    };
  };

  assert.equal(await auth.finishSignIn(), true);
  assert.equal(auth.currentAccessToken(), "renewed-access-token");
  const saved = JSON.parse(values.get("family-archive.oauth-session"));
  assert.equal(saved.refreshToken, "saved-refresh-token");
  assert.ok(saved.expiresAt > Date.now());
});

test("renews an access token shortly before a later API request", async () => {
  const auth = await freshAuthModule();
  values.set(
    "family-archive.oauth-transaction",
    JSON.stringify({ verifier: "pkce-verifier", state: "expected-state" }),
  );
  location.search = "?code=authorization-code&state=expected-state";
  const startedAt = Date.now();
  globalThis.fetch = async (url) => {
    assert.equal(url, "https://login.example.test/oauth2/token");
    return {
      ok: true,
      json: async () => ({
        access_token: "shortly-expiring-token",
        refresh_token: "saved-refresh-token",
        expires_in: 3600,
      }),
    };
  };
  assert.equal(await auth.finishSignIn(), true);

  const originalNow = Date.now;
  Date.now = () => startedAt + 3_570_000;
  try {
    globalThis.fetch = async (url, options) => {
      assert.equal(url, "https://login.example.test/oauth2/token");
      assert.match(String(options.body), /grant_type=refresh_token/);
      return {
        ok: true,
        json: async () => ({ access_token: "fresh-api-token", expires_in: 3600 }),
      };
    };
    assert.equal(await auth.getValidAccessToken(), "fresh-api-token");
  } finally {
    Date.now = originalNow;
  }
});

test("returns no API token when the refresh token is rejected", async () => {
  values.set(
    "family-archive.oauth-session",
    JSON.stringify({
      accessToken: "expired-access-token",
      refreshToken: "revoked-refresh-token",
      expiresAt: Date.now() - 1000,
    }),
  );
  location.search = "";
  const auth = await freshAuthModule();
  globalThis.fetch = async () => ({ ok: false, status: 400 });

  assert.equal(await auth.finishSignIn(), false);
  assert.equal(await auth.getValidAccessToken(), null);
  assert.equal(values.has("family-archive.oauth-session"), false);
});
