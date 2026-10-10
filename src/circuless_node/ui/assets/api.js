// Talking to this node.
//
// Paths are relative to the page, deliberately. The UI is served at `<base>/ui/`, so
// `../v1/x` resolves to `<base>/v1/x` — correct at the root and behind a gateway prefix
// alike, which an absolute `/v1/x` would get wrong.

import { REQUEST_ID_HEADER, forgetToken, token } from "./session.js";

/** A refusal the UI can show: the node's reason code is the contract, detail is a hint. */
export class Refused extends Error {
  constructor({ reason, detail, requestId, status }) {
    super(reason);
    this.reason = reason;
    this.detail = detail;
    this.requestId = requestId;
    this.status = status;
  }
}

/** Thrown when the token is gone or no longer accepted. The caller signs in again. */
export class NotSignedIn extends Error {}

async function describe(response) {
  let body = {};
  try {
    body = await response.json();
  } catch {
    // A proxy in front of the node can answer HTML. Falling through to the status code
    // is better than showing someone a parse error.
  }
  return new Refused({
    reason: body.reason || `http_${response.status}`,
    detail: body.detail || `The node answered ${response.status}.`,
    requestId: response.headers.get(REQUEST_ID_HEADER),
    status: response.status,
  });
}

/**
 * `params` is separate from `path` so that the path stays a route template. A test
 * asserts every path here is one the node actually serves, and a query string glued on
 * would make it a string no route can match — the check would then pass by comparing
 * nothing, which is the failure this codebase keeps finding.
 */
export async function get(path, params) {
  const query = new URLSearchParams(
    Object.entries(params || {}).filter(([, value]) => value !== undefined && value !== null),
  );
  const suffix = query.toString() ? `?${query}` : "";

  const response = await fetch(`../${path}${suffix}`, {
    headers: { Authorization: `Bearer ${token()}` },
  });

  if (response.ok) return response.json();

  if (response.status === 401) {
    // Expired, or meant for somewhere else. Either way this token is of no further use.
    forgetToken();
    throw new NotSignedIn();
  }
  throw await describe(response);
}

/** Everything that changes something. Same error contract as `get`. */
async function send(method, path, body) {
  const response = await fetch(`../${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (response.status === 204) return null;
  if (response.ok) return response.json();

  if (response.status === 401) {
    forgetToken();
    throw new NotSignedIn();
  }
  throw await describe(response);
}

/**
 * Upload, which is the one call that cannot use `fetch`.
 *
 * `fetch` reports no progress without plumbing a stream through a reader, and the
 * default limit here is a gibibyte — a form that sits silent for that long reads as
 * broken. XHR gives `upload.onprogress` for about twenty lines, and no build step.
 *
 * `onProgress` is called with a fraction, or with null when the browser cannot tell.
 */
export function upload(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", `../${path}`);
    request.setRequestHeader("Authorization", `Bearer ${token()}`);
    if (file.type) request.setRequestHeader("Content-Type", file.type);

    request.upload.onprogress = (event) =>
      onProgress(event.lengthComputable ? event.loaded / event.total : null);

    request.onload = () => {
      if (request.status === 401) {
        forgetToken();
        reject(new NotSignedIn());
        return;
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(JSON.parse(request.responseText || "null"));
        return;
      }
      let body = {};
      try {
        body = JSON.parse(request.responseText);
      } catch {
        // Same fallback as `describe`: a gateway in front of the node can answer HTML.
      }
      reject(new Refused({
        reason: body.reason || `http_${request.status}`,
        detail: body.detail || `The node answered ${request.status}.`,
        requestId: request.getResponseHeader(REQUEST_ID_HEADER),
        status: request.status,
      }));
    };

    // A dropped connection mid-upload. Distinguished from a refusal because the advice
    // differs: this one is worth simply trying again.
    request.onerror = () =>
      reject(new Refused({
        reason: "network_error",
        detail: "The upload did not reach the node. Check the connection and try again.",
        status: 0,
      }));

    request.send(file);
  });
}

export const whoami = () => get("v1/whoami");
export const nodeDocument = () => get(".well-known/circuless-node");
export const tenants = () => get("v1/tenants");
export const resources = (tenant) => get(`v1/t/${encodeURIComponent(tenant)}/resources`);
export const resource = (tenant, id) =>
  get(`v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}`);

export const accessLog = (tenant, params) =>
  get(`v1/t/${encodeURIComponent(tenant)}/access-log`, params);

export const credential = (tenant, id) =>
  get(`v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}/credential`);

export const setCredential = (tenant, id, body) =>
  send("PUT", `v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}/credential`,
    body);

export const removeCredential = (tenant, id) =>
  send("DELETE",
    `v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}/credential`);

export const withdrawResource = (tenant, id) =>
  send("DELETE", `v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}`);

/** `shape=file` takes the whole resource; `shape=bucket` takes one object by path. */
export const uploadPath = (tenant, id, objectPath) =>
  `v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}/data`
  + (objectPath ? `/${objectPath.split("/").map(encodeURIComponent).join("/")}` : "");

export const createResource = (tenant, body) =>
  send("POST", `v1/t/${encodeURIComponent(tenant)}/resources`, body);

export const patchResource = (tenant, id, body) =>
  send("PATCH", `v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}`, body);
