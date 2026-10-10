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

export async function get(path) {
  const response = await fetch(`../${path}`, {
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

export const whoami = () => get("v1/whoami");
export const tenants = () => get("v1/tenants");
export const resources = (tenant) => get(`v1/t/${encodeURIComponent(tenant)}/resources`);
export const resource = (tenant, id) =>
  get(`v1/t/${encodeURIComponent(tenant)}/resources/${encodeURIComponent(id)}`);
