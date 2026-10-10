// The node admin UI: sign in, then show which tenants you can reach here.
//
// No build step and no dependencies (CLAUDE.md). Plain ES modules, served from the
// node's own wheel, so a node on a partner's premises renders with nothing fetched
// from the internet — including this file.

const REQUEST_ID_HEADER = "X-CIRCULess-Request-Id";

// Tokens live in sessionStorage, not localStorage. Against script injected into this
// origin neither helps — an attacker who can run code here can simply read the variable
// — but sessionStorage dies with the tab and is not shared with another one, so a token
// does not outlive the window someone closed. The verifier is here too: it is single-use
// and worthless once the exchange has happened.
const store = window.sessionStorage;
const TOKEN_KEY = "circuless.access_token";
const VERIFIER_KEY = "circuless.pkce_verifier";
const STATE_KEY = "circuless.oauth_state";

const el = (id) => document.getElementById(id);

const show = (...ids) => {
  for (const id of ["loading", "signed-out", "signed-in", "problem"]) {
    el(id).hidden = !ids.includes(id);
  }
};

// --- problems ------------------------------------------------------------------------

/** Everything that can go wrong arrives here, so nothing fails silently into a blank page. */
function problem(title, detail, requestId) {
  el("problem-title").textContent = title;
  el("problem-detail").textContent = detail;
  el("problem-id").textContent = requestId ? `Request id: ${requestId}` : "";
  el("problem-id").hidden = !requestId;
  show("problem");
}

/** The node answers a reason code and an optional detail; the code is the contract. */
async function describe(response) {
  let body = {};
  try {
    body = await response.json();
  } catch {
    // A proxy or gateway in front of the node can answer HTML. Falling through to the
    // status code is better than showing someone a parse error.
  }
  const reason = body.reason || `http_${response.status}`;
  return {
    reason,
    detail: body.detail || `The node answered ${response.status}.`,
    requestId: response.headers.get(REQUEST_ID_HEADER),
  };
}

// --- PKCE ----------------------------------------------------------------------------

const randomString = () => {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return base64url(bytes);
};

function base64url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Where the identity provider sends the browser back. The UI's own URL, without the
 *  query string, so a second sign-in does not accumulate `?code=` on the end. */
const redirectUri = () => location.origin + location.pathname;

async function signIn(config) {
  const verifier = randomString();
  const state = randomString();
  store.setItem(VERIFIER_KEY, verifier);
  store.setItem(STATE_KEY, state);

  const query = new URLSearchParams({
    client_id: config.client_id,
    response_type: "code",
    redirect_uri: redirectUri(),
    // One audience per token: the scope names this node and nothing else, so the token
    // the UI holds cannot be replayed against the Cloud or another node.
    scope: config.scope,
    state,
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256",
  });
  location.assign(`${config.issuer}/protocol/openid-connect/auth?${query}`);
}

/** Completes the exchange if the browser came back with a code. Returns true if it did. */
async function completeSignIn(config) {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code && !params.get("error")) return false;

  // Clear the query immediately, whatever happens next: an authorization code in the
  // address bar survives a copied link and a browser history sync.
  history.replaceState(null, "", redirectUri());

  if (params.get("error")) {
    problem("Sign-in was refused", params.get("error_description") || params.get("error"));
    return true;
  }

  const expected = store.getItem(STATE_KEY);
  store.removeItem(STATE_KEY);
  if (!expected || params.get("state") !== expected) {
    problem(
      "Sign-in could not be completed",
      "The response did not match the request this tab started. Try signing in again.",
    );
    return true;
  }

  const verifier = store.getItem(VERIFIER_KEY);
  store.removeItem(VERIFIER_KEY);

  const response = await fetch(`${config.issuer}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.client_id,
      code,
      redirect_uri: redirectUri(),
      code_verifier: verifier,
    }),
  });

  if (!response.ok) {
    problem(
      "Sign-in could not be completed",
      "The identity provider rejected the exchange. Check that this node's UI URL is a "
        + "registered redirect URI for its client.",
    );
    return true;
  }

  store.setItem(TOKEN_KEY, (await response.json()).access_token);
  return true;
}

function signOut() {
  // Local only. Ending the Keycloak session as well would sign the person out of every
  // CIRCULess interface they have open, which is not what "sign out of this node" means.
  store.removeItem(TOKEN_KEY);
  location.assign(redirectUri());
}

// --- the node ------------------------------------------------------------------------

// Relative to the page, deliberately. The UI is served at `<base>/ui/`, so `../v1/x`
// resolves to `<base>/v1/x` — correct both at the root and behind a gateway prefix,
// which an absolute `/v1/x` would get wrong.
async function api(path) {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${store.getItem(TOKEN_KEY)}` },
  });
  if (response.ok) return response.json();

  if (response.status === 401) {
    // Expired, or meant for somewhere else. Either way this token is of no further use.
    store.removeItem(TOKEN_KEY);
    throw Object.assign(new Error("unauthenticated"), { unauthenticated: true });
  }
  throw Object.assign(new Error("refused"), await describe(response));
}

function renderTenants(tenants) {
  const list = el("tenants");
  list.replaceChildren();

  if (tenants.length === 0) {
    // Not an error. `stranger` in the fixture realm is exactly this: a real account
    // that no organisation has admitted yet, and saying so is more use than an
    // empty list that looks broken.
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent =
      "This node hosts no organisation you belong to. Ask an administrator of your "
      + "organisation to have it added here.";
    list.append(empty);
    el("tenants-intro").textContent = "";
    return;
  }

  el("tenants-intro").textContent =
    tenants.length === 1 ? "One organisation." : `${tenants.length} organisations.`;

  for (const tenant of tenants) {
    const item = document.createElement("li");
    item.className = "tenant";

    const slug = document.createElement("span");
    slug.className = "tenant__slug";
    slug.textContent = tenant.slug;

    const org = document.createElement("span");
    org.className = "tenant__org";
    org.textContent = tenant.org;

    const role = document.createElement("span");
    role.className = tenant.can_manage ? "tenant__role tenant__role--manage" : "tenant__role";
    // The wording matters: "member" has to read as a normal state, not as a failure.
    role.textContent = tenant.can_manage ? "Can manage" : "Member";

    item.append(slug, org, role);
    list.append(item);
  }
}

async function showSignedIn() {
  const [who, tenants] = await Promise.all([api("../v1/whoami"), api("../v1/tenants")]);

  // `sub` and the principal type, never a name or an email — a node token carries
  // neither, by design (D31).
  el("who").textContent = `${who.principal_type} · ${who.sub.slice(0, 8)}`;
  el("who").hidden = false;
  el("sign-out").hidden = false;

  renderTenants(tenants);
  show("signed-in");
}

// --- start ---------------------------------------------------------------------------

async function start() {
  show("loading");

  let config;
  try {
    const response = await fetch("config.json");
    if (!response.ok) throw new Error(String(response.status));
    config = await response.json();
  } catch {
    problem(
      "This node is not serving its UI configuration",
      "config.json could not be read. It is written when the node starts, so this "
        + "usually means the node's data directory is not writable.",
    );
    return;
  }

  el("node-name").textContent = config.node_id;
  el("node-name").hidden = false;

  el("sign-in").addEventListener("click", () => signIn(config));
  el("sign-out").addEventListener("click", signOut);
  el("problem-retry").addEventListener("click", () => location.assign(redirectUri()));

  if (await completeSignIn(config)) {
    if (!store.getItem(TOKEN_KEY)) return; // completeSignIn already explained why.
  }

  if (!store.getItem(TOKEN_KEY)) {
    show("signed-out");
    return;
  }

  try {
    await showSignedIn();
  } catch (error) {
    if (error.unauthenticated) {
      show("signed-out");
      return;
    }
    problem("The node refused the request", error.detail || String(error), error.requestId);
  }
}

start();
