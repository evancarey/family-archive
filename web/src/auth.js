import { getPresentationConfig } from "./config.js";

const TRANSACTION_KEY = "family-archive.oauth-transaction";
const SESSION_KEY = "family-archive.oauth-session";
const REFRESH_BEFORE_EXPIRY_MS = 60_000;
let accessToken = null;
let refreshToken = null;
let tokenExpiresAt = 0;
let refreshInFlight = null;

const base64Url = (bytes) =>
  btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");

const randomToken = (length = 32) => {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64Url(bytes);
};

const endpoint = (base, path) => `${base.replace(/\/+$/, "")}${path}`;

const saveSession = () => {
  sessionStorage.setItem(
    SESSION_KEY,
    JSON.stringify({ accessToken, refreshToken, expiresAt: tokenExpiresAt }),
  );
};

const clearSession = () => {
  accessToken = null;
  refreshToken = null;
  tokenExpiresAt = 0;
  sessionStorage.removeItem(SESSION_KEY);
};

const acceptTokens = (tokens, previousRefreshToken = null) => {
  if (
    typeof tokens.access_token !== "string" ||
    !Number.isFinite(tokens.expires_in) ||
    tokens.expires_in <= 0
  ) {
    throw new Error("The sign-in service returned an incomplete session.");
  }
  accessToken = tokens.access_token;
  refreshToken = typeof tokens.refresh_token === "string"
    ? tokens.refresh_token
    : previousRefreshToken;
  tokenExpiresAt = Date.now() + tokens.expires_in * 1000;
  saveSession();
};

const refreshAccessToken = async () => {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    const config = getPresentationConfig();
    const previousRefreshToken = refreshToken;
    const response = await fetch(endpoint(config.cognitoDomain, "/oauth2/token"), {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        client_id: config.clientId,
        refresh_token: previousRefreshToken,
      }),
    });
    if (!response.ok) {
      if (response.status === 400 || response.status === 401) {
        clearSession();
        return false;
      }
      throw new Error("Your sign-in session could not be renewed. Please try again.");
    }
    const tokens = await response.json();
    acceptTokens(tokens, previousRefreshToken);
    return true;
  })();
  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
};

const restoreSession = async () => {
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || "null");
  } catch {
    clearSession();
    return false;
  }
  if (
    !saved ||
    typeof saved.accessToken !== "string" ||
    !Number.isFinite(saved.expiresAt)
  ) {
    clearSession();
    return false;
  }
  accessToken = saved.accessToken;
  refreshToken = typeof saved.refreshToken === "string" ? saved.refreshToken : null;
  tokenExpiresAt = saved.expiresAt;
  if (tokenExpiresAt - Date.now() > REFRESH_BEFORE_EXPIRY_MS) return true;
  if (!refreshToken) {
    clearSession();
    return false;
  }
  return refreshAccessToken();
};

export const beginSignIn = async () => {
  const config = getPresentationConfig();
  const verifier = randomToken(48);
  const state = randomToken(32);
  const challenge = base64Url(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)),
  );
  sessionStorage.setItem(TRANSACTION_KEY, JSON.stringify({ verifier, state }));
  const authorize = new URL(endpoint(config.cognitoDomain, "/oauth2/authorize"));
  authorize.search = new URLSearchParams({
    client_id: config.clientId,
    response_type: "code",
    scope: (config.scopes || ["openid", "email"]).join(" "),
    redirect_uri: config.redirectUri,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  window.location.assign(authorize);
};

export const finishSignIn = async () => {
  const params = new URLSearchParams(window.location.search);
  if (params.has("error")) {
    sessionStorage.removeItem(TRANSACTION_KEY);
    window.history.replaceState({}, document.title, window.location.pathname);
    throw new Error("Sign-in was cancelled or declined.");
  }
  const code = params.get("code");
  if (!code) return restoreSession();

  const config = getPresentationConfig();
  const transaction = JSON.parse(sessionStorage.getItem(TRANSACTION_KEY) || "null");
  sessionStorage.removeItem(TRANSACTION_KEY);
  window.history.replaceState({}, document.title, window.location.pathname);
  if (!transaction || params.get("state") !== transaction.state) {
    throw new Error("Sign-in could not be verified. Please try again.");
  }
  const response = await fetch(endpoint(config.cognitoDomain, "/oauth2/token"), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.clientId,
      code,
      redirect_uri: config.redirectUri,
      code_verifier: transaction.verifier,
    }),
  });
  if (!response.ok) throw new Error("Sign-in could not be completed. Please try again.");
  acceptTokens(await response.json());
  return true;
};

export const currentAccessToken = () =>
  accessToken && tokenExpiresAt > Date.now() ? accessToken : null;

export const getValidAccessToken = async () => {
  if (!accessToken) return null;
  if (tokenExpiresAt - Date.now() > REFRESH_BEFORE_EXPIRY_MS) return accessToken;
  if (!refreshToken) {
    clearSession();
    return null;
  }
  return (await refreshAccessToken()) ? accessToken : null;
};

export const endSignIn = async () => {
  const config = getPresentationConfig();
  const token = refreshToken || accessToken;
  clearSession();
  sessionStorage.removeItem(TRANSACTION_KEY);
  if (token) {
    try {
      await fetch(endpoint(config.cognitoDomain, "/oauth2/revoke"), {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ token, client_id: config.clientId }),
      });
    } catch {
      // The hosted UI logout below still clears the browser's Cognito session.
    }
  }
  const logout = new URL(endpoint(config.cognitoDomain, "/logout"));
  logout.search = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: config.redirectUri,
  });
  window.location.assign(logout);
};
