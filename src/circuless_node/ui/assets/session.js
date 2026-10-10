// Signing in: config, PKCE, and where the token lives.
//
// Split out of app.js because it is the part with a security argument attached, and
// because the rest of the UI should only ever see "a token, or not".

export const REQUEST_ID_HEADER = "X-CIRCULess-Request-Id";

// Tokens live in sessionStorage, not localStorage. Against script injected into this
// origin neither helps — an attacker who can run code here can read the variable too —
// but sessionStorage dies with the tab and is not shared with another one, so a token
// does not outlive the window someone closed. The verifier is here as well: it is
// single-use and worthless once the exchange has happened.
const store = window.sessionStorage;
const TOKEN_KEY = "circuless.access_token";
const VERIFIER_KEY = "circuless.pkce_verifier";
const STATE_KEY = "circuless.oauth_state";

export const token = () => store.getItem(TOKEN_KEY);
export const forgetToken = () => store.removeItem(TOKEN_KEY);

/** Where the identity provider sends the browser back: the UI's own URL, with neither
 *  query string nor fragment, so a second sign-in does not accumulate state on the end
 *  and the registered redirect URI stays a single fixed string. */
export const redirectUri = () => location.origin + location.pathname;

export async function loadConfig() {
  const response = await fetch("config.json");
  if (!response.ok) throw new Error(`config.json answered ${response.status}`);
  return response.json();
}

// --- PKCE ----------------------------------------------------------------------------

function base64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

const randomString = () => base64url(crypto.getRandomValues(new Uint8Array(32)));

async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

export async function signIn(config) {
  const verifier = randomString();
  const state = randomString();
  store.setItem(VERIFIER_KEY, verifier);
  store.setItem(STATE_KEY, state);

  const query = new URLSearchParams({
    client_id: config.client_id,
    response_type: "code",
    redirect_uri: redirectUri(),
    // One audience per token: the scope names this node and nothing else, so what the
    // UI holds cannot be replayed against the Cloud or another node.
    scope: config.scope,
    state,
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
  });
  location.assign(`${config.issuer}/protocol/openid-connect/auth?${query}`);
}

/**
 * Finishes the exchange if the browser came back from the identity provider.
 * Returns null if there was nothing to finish, otherwise {ok} or {ok: false, detail}.
 */
export async function completeSignIn(config) {
  const params = new URLSearchParams(location.search);
  if (!params.get("code") && !params.get("error")) return null;

  // Clear the query immediately, whatever happens next: an authorization code in the
  // address bar survives a copied link and a browser history sync.
  history.replaceState(null, "", redirectUri());

  if (params.get("error")) {
    return { ok: false, detail: params.get("error_description") || params.get("error") };
  }

  const expected = store.getItem(STATE_KEY);
  store.removeItem(STATE_KEY);
  if (!expected || params.get("state") !== expected) {
    return {
      ok: false,
      detail: "The response did not match the request this tab started. Sign in again.",
    };
  }

  const verifier = store.getItem(VERIFIER_KEY);
  store.removeItem(VERIFIER_KEY);

  const response = await fetch(`${config.issuer}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.client_id,
      code: params.get("code"),
      redirect_uri: redirectUri(),
      code_verifier: verifier,
    }),
  });

  if (!response.ok) {
    return {
      ok: false,
      detail:
        "The identity provider rejected the exchange. Check that this node's UI URL is a "
        + "registered redirect URI and web origin for its client.",
    };
  }

  store.setItem(TOKEN_KEY, (await response.json()).access_token);
  return { ok: true };
}

export function signOut() {
  // Local only. Ending the Keycloak session as well would sign the person out of every
  // CIRCULess interface they have open, which is not what "sign out of this node" means.
  forgetToken();
  location.assign(redirectUri());
}
