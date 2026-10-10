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
import { fieldsFor } from "./fields.js";
import { completeSignIn, forgetToken, loadConfig, redirectUri, signIn, signOut, token }
  from "./session.js";

const el = (id) => document.getElementById(id);
const VIEWS = ["loading", "signed-out", "tenants-view", "resources-view", "resource-view",
  "log-view", "form-view", "credential-view", "problem"];

/** `capabilities.accepts_sensitive` from `/.well-known`, read once after sign-in. */
let nodeCapabilities = {};

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

  el("resources-log-link").href = `#/t/${encodeURIComponent(tenant)}/log`;
  el("resources-new-link").href = `#/t/${encodeURIComponent(tenant)}/new`;
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

  el("resource-log-link").href =
    `#/t/${encodeURIComponent(tenant)}/log?resource=${encodeURIComponent(resource.id)}`;
  el("resource-edit-link").href =
    `#/t/${encodeURIComponent(tenant)}/r/${encodeURIComponent(resource.id)}/edit`;

  const live = resource.status !== WITHDRAWN;

  // A service's upstream credential; a dataset has none to set.
  const credentialLink = el("resource-credential-link");
  credentialLink.hidden = resource.kind !== "service" || !live;
  credentialLink.href =
    `#/t/${encodeURIComponent(tenant)}/r/${encodeURIComponent(resource.id)}/credential`;

  setUpUpload(tenant, resource, live);
  setUpWithdraw(tenant, resource, live);

  show("resource-view");
}

// --- the access log --------------------------------------------------------------------

const PAGE = 100;

/** Only the two the node accepts; anything else is "all" (N11's filter is `allow|deny`). */
const DECISIONS = ["allow", "deny"];

async function logView(tenant, params) {
  const offset = Math.max(0, Number.parseInt(params.get("offset") || "0", 10) || 0);
  const resourceId = params.get("resource") || undefined;
  const decision = DECISIONS.includes(params.get("decision")) ? params.get("decision") : undefined;

  const page = await api.accessLog(tenant, {
    limit: PAGE,
    offset,
    resource_id: resourceId,
    decision,
  });
  const entries = page.entries;

  setCrumbs([
    { label: "Organisations", hash: "#/" },
    { label: tenant, hash: `#/t/${encodeURIComponent(tenant)}/resources` },
    { label: "Access log" },
  ]);

  el("log-scope").textContent = resourceId
    ? `One resource of ${tenant}.`
    : `Every decision about ${tenant}'s resources, newest first.`;

  // The filter chips keep whatever else is in the URL, so narrowing to one resource and
  // then to denials does not silently drop the resource.
  const withFilter = (value) => {
    const next = new URLSearchParams(params);
    next.delete("offset"); // a new filter starts at the first page, not page seven
    if (value) next.set("decision", value);
    else next.delete("decision");
    return `#/t/${encodeURIComponent(tenant)}/log?${next}`;
  };
  el("log-filter-all").href = withFilter(null);
  el("log-filter-allow").href = withFilter("allow");
  el("log-filter-deny").href = withFilter("deny");
  for (const [id, value] of [["all", undefined], ["allow", "allow"], ["deny", "deny"]]) {
    el(`log-filter-${id}`).classList.toggle("chip--on", decision === value);
  }

  const body = el("log-body");
  body.replaceChildren();
  el("log-empty").hidden = entries.length > 0;
  el("log-table").hidden = entries.length === 0;

  for (const entry of entries) {
    const row = fill(body, "tr", entry.decision === "deny" ? "row--deny" : "");

    fill(row, "td", "mono", entry.ts);

    const action = fill(row, "td");
    fill(action, "div", "", entry.action);
    if (entry.resource_id) fill(action, "div", "cell__sub mono", entry.resource_id);

    const decisionCell = fill(row, "td");
    fill(decisionCell, "span", `pill pill--${entry.decision}`, entry.decision);
    if (entry.reason) fill(decisionCell, "div", "cell__sub", entry.reason);

    // A pseudonymous subject and a principal type. There is no name or email to show:
    // a node token does not carry one (D31), which is why this column is a uuid.
    const who = fill(row, "td");
    fill(who, "div", "mono", entry.subject_sub || "—");
    fill(who, "div", "cell__sub", entry.principal_type + (entry.actor ? ` · ${entry.actor}` : ""));

    fill(row, "td", "", entry.acting_org || "—");
    fill(row, "td", "", entry.bytes === null || entry.bytes === undefined ? "—" : entry.bytes);
    fill(row, "td", "mono", entry.request_id || "—");
  }

  // The node returns no total — it is an investigation tool, not a report — so "there
  // is a next page" is inferred from a full page having come back. A last page that is
  // exactly full shows a Next that lands on an empty one; that is the honest cost of
  // not making the node count rows it does not need to count.
  const pageHref = (at) => {
    const next = new URLSearchParams(params);
    if (at > 0) next.set("offset", String(at));
    else next.delete("offset");
    return `#/t/${encodeURIComponent(tenant)}/log?${next}`;
  };
  const prev = el("log-prev");
  const next = el("log-next");

  prev.href = pageHref(Math.max(0, offset - PAGE));
  prev.classList.toggle("is-disabled", offset === 0);
  next.href = pageHref(offset + PAGE);
  next.classList.toggle("is-disabled", entries.length < PAGE);

  el("log-range").textContent = entries.length
    ? `Entries ${offset + 1}–${offset + entries.length}`
    : "No entries";

  show("log-view");
}

// --- uploading bytes ------------------------------------------------------------------

function setUpUpload(tenant, resource, live) {
  const panel = el("resource-upload");
  // Only a dataset holds bytes, and only while it is still live: writing to a withdrawn
  // resource would be writing to something scheduled for purge.
  panel.hidden = resource.kind !== "dataset" || !live;
  if (panel.hidden) return;

  const bucket = resource.shape === "bucket";
  el("resource-object-row").hidden = !bucket;
  el("resource-upload-hint").textContent = bucket
    ? "Each object is uploaded and decided separately."
    : "Uploading replaces this resource's bytes.";

  const file = el("resource-file");
  const status = el("resource-upload-status");
  const bar = el("resource-progress");
  const go = el("resource-upload-go");

  file.value = "";
  status.textContent = "";
  bar.hidden = true;

  go.onclick = async () => {
    const chosen = file.files[0];
    if (!chosen) {
      status.textContent = "Choose a file first.";
      return;
    }
    const objectPath = bucket ? el("resource-object-path").value.trim() : "";
    if (bucket && !objectPath) {
      status.textContent = "A bucket object needs a path.";
      return;
    }

    go.disabled = true;
    bar.hidden = false;
    bar.removeAttribute("value"); // indeterminate until the first progress event
    status.textContent = `Uploading ${chosen.name}…`;

    try {
      const result = await api.upload(
        api.uploadPath(tenant, resource.id, objectPath),
        chosen,
        (fraction) => {
          if (fraction === null) bar.removeAttribute("value");
          else bar.value = fraction;
        },
      );
      status.textContent = `Stored ${result.bytes} bytes at ${result.path}.`;
      bar.hidden = true;
      file.value = "";
    } catch (error) {
      bar.hidden = true;
      if (error instanceof NotSignedIn) {
        show("signed-out");
        return;
      }
      status.textContent = error instanceof Refused
        ? `${error.detail} (${error.reason})`
        : String(error.message || error);
    } finally {
      go.disabled = false;
    }
  };
}

// --- withdrawing -----------------------------------------------------------------------

function setUpWithdraw(tenant, resource, live) {
  const button = el("resource-withdraw");
  const confirm = el("withdraw-confirm");
  const typed = el("withdraw-slug");
  const go = el("withdraw-go");
  const error = el("withdraw-error");

  button.hidden = !live;
  confirm.hidden = true;
  error.hidden = true;
  typed.value = "";
  go.disabled = true;

  el("withdraw-explain").textContent =
    `Consumers lose access immediately and the catalogue record is withdrawn. The data `
    + `is not removed at once — the node keeps it until its purge date so a mistake can `
    + `be undone, and access log entries are kept either way.`;

  button.onclick = () => {
    confirm.hidden = false;
    typed.focus();
  };
  el("withdraw-cancel").onclick = () => {
    confirm.hidden = true;
  };

  // Typing the slug, not a bare confirmation: the point is to make someone read which
  // resource this is while a list of similarly named ones is a click away.
  typed.oninput = () => {
    go.disabled = typed.value.trim() !== resource.slug;
  };

  go.onclick = async () => {
    go.disabled = true;
    error.hidden = true;
    try {
      await api.withdrawResource(tenant, resource.id);
      await render();
    } catch (failure) {
      if (failure instanceof NotSignedIn) {
        show("signed-out");
        return;
      }
      error.textContent = failure instanceof Refused
        ? `${failure.detail} (${failure.reason})`
        : String(failure.message || failure);
      error.hidden = false;
      go.disabled = false;
    }
  };
}

// --- the upstream credential ---------------------------------------------------------

/** `header` needs a header name, `basic` needs a username, `bearer` needs neither. */
const SCHEMES = ["bearer", "header", "basic"];

async function credentialView(tenant, resourceId) {
  const resource = await api.resource(tenant, resourceId);
  const backTo = `#/t/${encodeURIComponent(tenant)}/r/${encodeURIComponent(resourceId)}`;

  setCrumbs([
    { label: "Organisations", hash: "#/" },
    { label: tenant, hash: `#/t/${encodeURIComponent(tenant)}/resources` },
    { label: resource.slug, hash: backTo },
    { label: "Credential" },
  ]);

  el("credential-intro").textContent =
    `What the node sends upstream when it forwards a call to ${resource.slug}.`;
  el("credential-cancel").href = backTo;
  el("credential-error").hidden = true;
  el("credential-secret").value = "";

  // 404 is the ordinary answer here — it means none is set — so it is read as state
  // rather than reported as a failure.
  let current = null;
  try {
    current = await api.credential(tenant, resourceId);
  } catch (error) {
    if (error instanceof NotSignedIn) {
      show("signed-out");
      return;
    }
    if (!(error instanceof Refused) || error.status !== 404) throw error;
  }

  el("credential-current").textContent = current
    ? `A ${current.scheme} credential is set`
      + (current.header_name ? ` on ${current.header_name}` : "")
      + `, last updated ${current.updated_at}.`
    : "No credential is set. The node forwards calls to this service unauthenticated.";
  el("credential-remove").hidden = !current;
  el("credential-save").textContent = current ? "Rotate credential" : "Set credential";

  const scheme = el("credential-scheme");
  scheme.replaceChildren();
  for (const name of SCHEMES) {
    const option = fill(scheme, "option", "", name);
    option.value = name;
  }
  scheme.value = current ? current.scheme : "bearer";

  // Structural, like the resource form: the node refuses a username on a bearer and a
  // header name on a basic, so neither is offered where it does not belong.
  const showForScheme = () => {
    el("credential-username-row").hidden = scheme.value !== "basic";
    el("credential-header-row").hidden = scheme.value !== "header";
  };
  scheme.onchange = showForScheme;
  showForScheme();

  show("credential-view");

  el("credential-form").onsubmit = async (event) => {
    event.preventDefault();
    el("credential-error").hidden = true;
    el("credential-save").disabled = true;

    const body = { scheme: scheme.value, secret: el("credential-secret").value };
    if (scheme.value === "basic") body.username = el("credential-username").value.trim();
    if (scheme.value === "header") body.header_name = el("credential-header").value.trim();

    try {
      await api.setCredential(tenant, resourceId, body);
      location.assign(backTo);
    } catch (error) {
      if (error instanceof NotSignedIn) {
        show("signed-out");
        return;
      }
      el("credential-error").textContent = error instanceof Refused
        ? `${error.detail} (${error.reason})`
        : String(error.message || error);
      el("credential-error").hidden = false;
    } finally {
      el("credential-save").disabled = false;
    }
  };

  el("credential-remove").onclick = async () => {
    try {
      await api.removeCredential(tenant, resourceId);
      await render();
    } catch (error) {
      el("credential-error").textContent = error instanceof Refused ? error.detail : String(error);
      el("credential-error").hidden = false;
    }
  };
}

// --- the resource form -------------------------------------------------------------------

/** Builds one labelled control. Returns the input so the caller can read it back. */
function renderField(parent, field, value) {
  const row = fill(parent, "div", "field");
  const label = fill(row, "label", "field__label", field.label + (field.required ? " *" : ""));
  label.htmlFor = `f-${field.name}`;

  let input;
  if (field.type === "select") {
    input = fill(row, "select", "field__input");
    if (field.blank !== undefined || !field.required) {
      const blank = fill(input, "option", "", field.blank || "—");
      blank.value = "";
    }
    for (const option of field.options) {
      const node = fill(input, "option", "", option);
      node.value = option;
      // D22: a BVR-operated node refuses sensitive data. The node still refuses it if
      // this is bypassed — this only stops someone filling in a long form to be told so.
      if (field.name === "classification" && option === "sensitive"
          && nodeCapabilities.accepts_sensitive === false) {
        node.disabled = true;
        node.textContent = "sensitive — this node does not hold sensitive data";
      }
    }
  } else if (field.type === "textarea" || field.type === "json") {
    input = fill(row, "textarea", "field__input");
    input.rows = field.type === "json" ? 6 : 3;
  } else {
    input = fill(row, "input", "field__input");
    input.type = "text";
  }

  input.id = `f-${field.name}`;
  input.name = field.name;
  if (value !== null && value !== undefined) {
    input.value = field.type === "json" ? JSON.stringify(value, null, 2) : String(value);
  }
  if (field.hint) fill(row, "p", "field__hint", field.hint);
  return input;
}

/** Reads the form back. Throws on malformed JSON so it is reported like any refusal. */
function readForm(fields, inputs, { creating, original }) {
  const body = {};

  for (const field of fields) {
    const raw = inputs[field.name].value.trim();

    let value = raw === "" ? null : raw;
    if (value !== null && field.type === "json") {
      try {
        value = JSON.parse(raw);
      } catch (error) {
        throw new Error(`${field.label} is not valid JSON: ${error.message}`);
      }
    }

    if (creating) {
      // Absent means "the node's default", which is the closed end (NFR4). Sending null
      // would be asking for null, which is a different thing.
      if (value !== null) body[field.name] = value;
      continue;
    }

    // Editing: send only what changed. `ResourcePatch` reads an absent field as "leave
    // it alone", so sending everything back would rewrite fields nobody touched — and
    // would turn a field someone cleared into a null the node cannot tell from untouched.
    const before = original[field.name];
    const unchanged = (before === null || before === undefined ? null : before) === value
      || JSON.stringify(before ?? null) === JSON.stringify(value);
    if (!unchanged) body[field.name] = value;
  }

  return body;
}

async function formView(tenant, resourceId) {
  const creating = !resourceId;
  const original = creating ? {} : await api.resource(tenant, resourceId);

  const backTo = creating
    ? `#/t/${encodeURIComponent(tenant)}/resources`
    : `#/t/${encodeURIComponent(tenant)}/r/${encodeURIComponent(resourceId)}`;

  setCrumbs([
    { label: "Organisations", hash: "#/" },
    { label: tenant, hash: `#/t/${encodeURIComponent(tenant)}/resources` },
    creating ? { label: "Register" } : { label: original.slug, hash: backTo },
    ...(creating ? [] : [{ label: "Edit" }]),
  ]);

  el("form-title").textContent = creating ? "Register a resource" : `Edit ${original.slug}`;
  el("form-intro").textContent = creating
    ? "A new resource is hidden and visible to your organisation only until you say otherwise."
    : "The slug and the kind cannot be changed; registering a different one is the way.";
  el("form-error").hidden = true;
  el("form-submit").textContent = creating ? "Register" : "Save changes";
  el("form-cancel").href = backTo;

  const container = el("form-fields");
  let inputs = {};
  let fields = [];

  const draw = (kind) => {
    const kept = Object.fromEntries(
      Object.entries(inputs).map(([name, input]) => [name, input.value]),
    );
    container.replaceChildren();
    fields = fieldsFor(kind, { creating });
    inputs = {};
    for (const field of fields) {
      inputs[field.name] = renderField(container, field, original[field.name]);
      // Redrawing on a kind change must not empty what has already been typed.
      if (kept[field.name] !== undefined) inputs[field.name].value = kept[field.name];
    }
    if (creating) {
      inputs.kind.value = kind;
      inputs.kind.addEventListener("change", () => draw(inputs.kind.value));
    }
  };

  draw(creating ? "dataset" : original.kind);
  show("form-view");

  el("form").onsubmit = async (event) => {
    event.preventDefault();
    el("form-error").hidden = true;
    el("form-submit").disabled = true;

    try {
      const body = readForm(fields, inputs, { creating, original });

      if (!creating && Object.keys(body).length === 0) {
        location.assign(backTo);
        return;
      }

      const saved = creating
        ? await api.createResource(tenant, body)
        : await api.patchResource(tenant, resourceId, body);

      location.assign(`#/t/${encodeURIComponent(tenant)}/r/${encodeURIComponent(saved.id)}`);
    } catch (error) {
      if (error instanceof NotSignedIn) {
        show("signed-out");
        return;
      }
      // Shown in the form, not as a page: the node's 422s name a field, and the person
      // needs to be looking at that field while they read the message.
      el("form-error").textContent = error instanceof Refused
        ? `${error.detail} (${error.reason})`
        : error.message;
      el("form-error").hidden = false;
    } finally {
      el("form-submit").disabled = false;
    }
  };
}

// --- routing -----------------------------------------------------------------------------

/**
 * `#/t/<slug>/resources`, `#/t/<slug>/r/<id>`, `#/t/<slug>/log?decision=deny&offset=100`.
 * Anything else is the tenant list.
 *
 * Filters live in the fragment rather than in a variable so that the back button, a
 * reload and a pasted link all show the same thing — which, for a page whose job is to
 * answer "what happened", is most of its value.
 */
function route() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(query || "");

  if (parts[0] === "t" && parts[2] === "resources") return resourcesView(parts[1]);
  if (parts[0] === "t" && parts[2] === "r" && parts[3] && !parts[4]) {
    return resourceView(parts[1], parts[3]);
  }
  if (parts[0] === "t" && parts[2] === "log") return logView(parts[1], params);
  if (parts[0] === "t" && parts[2] === "new") return formView(parts[1], null);
  if (parts[0] === "t" && parts[2] === "r" && parts[4] === "edit") {
    return formView(parts[1], parts[3]);
  }
  if (parts[0] === "t" && parts[2] === "r" && parts[4] === "credential") {
    return credentialView(parts[1], parts[3]);
  }
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

    // What this node will and will not hold (D22). Token-gated, so it is read here
    // rather than from config.json, which may carry no node state.
    nodeCapabilities = (await api.nodeDocument()).capabilities || {};
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
