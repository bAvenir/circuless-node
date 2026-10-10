// The node admin UI: which tenants you can reach here, and what each one holds.
//
// No build step and no dependencies (CLAUDE.md). Plain ES modules served from the node's
// own wheel, so a node on a partner's premises renders with nothing fetched from the
// internet — including this file.
//
// Routing is in the fragment (`#/t/alpha/resources`), not the path. A static mount
// cannot answer `/ui/t/alpha/resources`, so a path router would 404 on every reload and
// every shared link; the fragment never reaches the server.

import * as api from "./api.js";
import { NotSignedIn, Refused } from "./api.js";
import { completeSignIn, forgetToken, loadConfig, redirectUri, signIn, signOut, token }
  from "./session.js";

const el = (id) => document.getElementById(id);
const VIEWS = ["loading", "signed-out", "tenants-view", "resources-view", "resource-view",
  "problem"];

function show(id) {
  for (const view of VIEWS) el(view).hidden = view !== id;
}

/** Text, never markup. Every string below is either the node's or a person's. */
function fill(parent, tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  parent.append(node);
  return node;
}

// --- problems ------------------------------------------------------------------------

function problem(title, detail, requestId) {
  el("problem-title").textContent = title;
  el("problem-detail").textContent = detail;
  el("problem-id").textContent = requestId ? `Request id: ${requestId}` : "";
  el("problem-id").hidden = !requestId;
  show("problem");
}

/** Everything a view can throw ends up here, so nothing fails into a blank page. */
function showFailure(error) {
  if (error instanceof NotSignedIn) {
    show("signed-out");
    return;
  }
  if (error instanceof Refused) {
    problem(titleFor(error), error.detail, error.requestId);
    return;
  }
  problem("Something went wrong", String(error && error.message ? error.message : error));
}

/** The refusals worth naming. Anything else keeps the node's own wording. */
function titleFor(error) {
  if (error.status === 403) return "You may not do that";
  if (error.status === 404) return "Not found";
  return "The node refused the request";
}

// --- the chrome ------------------------------------------------------------------------

function setCrumbs(parts) {
  const bar = el("crumbs");
  bar.replaceChildren();
  parts.forEach((part, index) => {
    if (index > 0) fill(bar, "span", "crumbs__sep", "/");
    if (part.hash === undefined) {
      fill(bar, "span", "crumbs__here", part.label);
      return;
    }
    const link = fill(bar, "a", "crumbs__link", part.label);
    link.href = part.hash;
  });
  bar.hidden = parts.length === 0;
}

// --- tenants ---------------------------------------------------------------------------

async function tenantsView() {
  const tenants = await api.tenants();
  setCrumbs([]);

  const list = el("tenants");
  list.replaceChildren();

  el("tenants-intro").textContent =
    tenants.length === 1 ? "One organisation." : `${tenants.length} organisations.`;

  if (tenants.length === 0) {
    // Not an error. A real account that no organisation has admitted yet is exactly
    // this, and saying so is more use than an empty list that looks broken.
    el("tenants-intro").textContent = "";
    fill(list, "li", "empty",
      "This node hosts no organisation you belong to. Ask an administrator of your "
      + "organisation to have it added here.");
    show("tenants-view");
    return;
  }

  for (const tenant of tenants) {
    const item = fill(list, "li", "tenant");

    // Membership and management are different rights (N18). A member who is not an
    // admin can reach nothing behind this link, so it is not a link — saying why is
    // kinder than a 403 one click later.
    if (tenant.can_manage) {
      const link = fill(item, "a", "tenant__slug", tenant.slug);
      link.href = `#/t/${encodeURIComponent(tenant.slug)}/resources`;
    } else {
      fill(item, "span", "tenant__slug tenant__slug--plain", tenant.slug);
    }

    fill(item, "span", "tenant__org", tenant.org);
    fill(item, "span",
      tenant.can_manage ? "tenant__role tenant__role--manage" : "tenant__role",
      tenant.can_manage ? "Can manage" : "Member, cannot manage");
  }
  show("tenants-view");
}

// --- resources ---------------------------------------------------------------------------

const WITHDRAWN = "withdrawn";

async function resourcesView(tenant) {
  const resources = await api.resources(tenant);
  setCrumbs([{ label: "Organisations", hash: "#/" }, { label: tenant }]);
  el("resources-title").textContent = tenant;

  const body = el("resources-body");
  body.replaceChildren();

  el("resources-empty").hidden = resources.length > 0;
  el("resources-table").hidden = resources.length === 0;
  el("resources-count").textContent =
    resources.length === 1 ? "One resource." : `${resources.length} resources.`;

  for (const resource of resources) {
    const row = fill(body, "tr", resource.status === WITHDRAWN ? "row--withdrawn" : "");

    const nameCell = fill(row, "td");
    const link = fill(nameCell, "a", "mono", resource.slug);
    link.href = `#/t/${encodeURIComponent(tenant)}/r/${encodeURIComponent(resource.id)}`;
    if (resource.title) fill(nameCell, "div", "cell__sub", resource.title);

    fill(row, "td", "", resource.kind);
    fill(row, "td", "", resource.shape);

    const visibility = fill(row, "td");
    fill(visibility, "span", "pill", resource.visibility);

    const status = fill(row, "td");
    fill(status, "span", `pill pill--${resource.status}`, resource.status);
  }
  show("resources-view");
}

// --- one resource -------------------------------------------------------------------------

/** Fields shown for every resource, in the order someone reads them. */
const FIELDS = [
  ["Title", (r) => r.title],
  ["Description", (r) => r.description],
  ["Kind", (r) => r.kind],
  ["Shape", (r) => r.shape],
  ["Theme", (r) => r.theme],
  ["Licence", (r) => r.licence],
  ["Classification", (r) => r.classification],
  ["Visibility", (r) => r.visibility],
  ["Discoverability", (r) => r.discoverability],
  ["Storage path", (r) => r.storage_path],
  // Management-only, and it stays that way: a consumer that knew the upstream address
  // could go round the node, past decide(), past the agreement check and past the log.
  ["Upstream endpoint", (r) => r.endpoint_url],
  ["OpenAPI", (r) => r.openapi_ref],
  ["Created", (r) => r.created_at],
  ["Updated", (r) => r.updated_at],
];

async function resourceView(tenant, id) {
  const resource = await api.resource(tenant, id);
  setCrumbs([
    { label: "Organisations", hash: "#/" },
    { label: tenant, hash: `#/t/${encodeURIComponent(tenant)}/resources` },
    { label: resource.slug },
  ]);

  el("resource-slug").textContent = resource.slug;
  el("resource-id").textContent = resource.id;

  // Two-stage deletion (N20, D25). A withdrawn resource is gone to every consumer
  // already; the owner sees it here precisely so the pending purge is not a surprise.
  const notice = el("resource-withdrawn");
  notice.hidden = resource.status !== WITHDRAWN;
  if (resource.status === WITHDRAWN) {
    notice.textContent = resource.purge_after
      ? `Withdrawn ${resource.withdrawn_at}. Data and metadata are removed after `
        + `${resource.purge_after}. Access log entries are kept.`
      : `Withdrawn ${resource.withdrawn_at}.`;
  }

  const fields = el("resource-fields");
  fields.replaceChildren();
  for (const [label, read] of FIELDS) {
    const value = read(resource);
    if (value === null || value === undefined || value === "") continue;
    fill(fields, "dt", "", label);
    fill(fields, "dd", "", String(value));
  }

  const policy = el("resource-policy");
  policy.hidden = !resource.invoke_policy;
  if (resource.invoke_policy) {
    el("resource-policy-body").textContent = JSON.stringify(resource.invoke_policy, null, 2);
  }

  show("resource-view");
}

// --- routing -----------------------------------------------------------------------------

/** `#/t/<slug>/resources` and `#/t/<slug>/r/<id>`; anything else is the tenant list. */
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);

  if (parts[0] === "t" && parts[2] === "resources") return resourcesView(parts[1]);
  if (parts[0] === "t" && parts[2] === "r" && parts[3]) return resourceView(parts[1], parts[3]);
  return tenantsView();
}

async function render() {
  if (!token()) {
    show("signed-out");
    return;
  }
  show("loading");
  try {
    await route();
  } catch (error) {
    showFailure(error);
  }
}

// --- start ---------------------------------------------------------------------------------

async function start() {
  show("loading");

  let config;
  try {
    config = await loadConfig();
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
  window.addEventListener("hashchange", render);

  const completed = await completeSignIn(config);
  if (completed && !completed.ok) {
    problem("Sign-in could not be completed", completed.detail);
    return;
  }

  if (!token()) {
    show("signed-out");
    return;
  }

  try {
    const who = await api.whoami();
    // Principal type and a truncated sub — never a name or an email, because a node
    // token carries neither, by design (D31).
    el("who").textContent = `${who.principal_type} · ${who.sub.slice(0, 8)}`;
    el("who").hidden = false;
    el("sign-out").hidden = false;
  } catch (error) {
    if (error instanceof NotSignedIn) {
      forgetToken();
      show("signed-out");
      return;
    }
    showFailure(error);
    return;
  }

  await render();
}

start();
