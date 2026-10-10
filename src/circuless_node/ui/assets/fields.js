// What a registration form is made of.
//
// The vocabularies below are duplicated from the node's Python enums. That is a drift
// risk taken deliberately: there is no endpoint that publishes them, and inventing one
// to avoid a hardcoded list would put a disclosure on the wire to save a test. Each list
// is pinned against its Python source in `test_ui_assets.py`, so adding a Theme without
// adding it here fails the suite.
//
// Only *structure* lives here. Which fields exist for which kind is the form's business;
// whether a value is allowed is the node's, and its refusals are shown as they come.
// So `public` discoverability appears in the list even though the node always refuses it
// (D21 — the beta has no anonymous access): leaving it out would be this file deciding a
// value rule, and the node's message says more than a missing option would.

export const KINDS = ["dataset", "service"];
export const DATASET_SHAPES = ["file", "bucket"];
export const THEMES = [
  "material-characterisation",
  "processing",
  "product-development",
  "value-chain",
];
export const CLASSIFICATIONS = ["synthetic", "non-sensitive", "sensitive"];
export const VISIBILITIES = ["private", "org", "agreement", "public"];
export const DISCOVERABILITIES = ["hidden", "catalogue", "public"];
export const LICENCES = [
  "CC-BY-4.0",
  "CC-BY-SA-4.0",
  "CC-BY-NC-4.0",
  "CC0-1.0",
  "ODbL-1.0",
  "Apache-2.0",
  "MIT",
  "EUPL-1.2",
  "COM_REUSE",
  "CIRCULESS-AGREEMENT-ONLY",
];

/**
 * Every field a resource form can show.
 *
 * `kinds` is the structural rule, and it mirrors `ResourceIn.coherent_for_its_kind`:
 * the node refuses `endpoint_url` on a dataset and `storage_path` on a service, so the
 * form does not offer them. `create` marks the two the node will not let you change
 * afterwards — `slug` is how a provider's scripts name the resource, and `kind` decides
 * which DCAT-AP type the catalogue already published.
 */
export const FIELDS = [
  { name: "slug", label: "Slug", type: "text", create: true, required: true,
    hint: "Lowercase, how scripts will refer to this. Cannot be changed later." },
  { name: "kind", label: "Kind", type: "select", options: KINDS, create: true, required: true,
    hint: "Cannot be changed later." },
  { name: "title", label: "Title", type: "text", required: true },
  { name: "description", label: "Description", type: "textarea" },
  { name: "theme", label: "Theme", type: "select", options: THEMES, required: true },
  { name: "classification", label: "Classification", type: "select",
    options: CLASSIFICATIONS, required: true },
  { name: "licence", label: "Licence", type: "select", options: LICENCES, blank: "None",
    hint: "Required before this can be discoverable." },
  { name: "discoverability", label: "Discoverability", type: "select",
    options: DISCOVERABILITIES, blank: "Default (hidden)" },
  { name: "visibility", label: "Visibility", type: "select", options: VISIBILITIES,
    blank: "Default (organisation)" },

  { name: "shape", label: "Shape", type: "select", options: DATASET_SHAPES,
    kinds: ["dataset"], blank: "Default (file)", create: true },
  { name: "storage_path", label: "Storage path", type: "text", kinds: ["dataset"] },

  { name: "endpoint_url", label: "Upstream endpoint", type: "text", kinds: ["service"],
    required: true,
    hint: "Where the node forwards invocations. Never published to the catalogue." },
  { name: "openapi_ref", label: "OpenAPI reference", type: "text", kinds: ["service"] },
  { name: "invoke_policy", label: "Invoke policy", type: "json", kinds: ["service"],
    hint: "JSON. Leave empty for the node's defaults." },
];

/** The fields a form shows for this kind, at this moment. */
export function fieldsFor(kind, { creating }) {
  return FIELDS.filter((field) => {
    if (field.kinds && !field.kinds.includes(kind)) return false;
    if (field.create && !creating) return false;
    return true;
  });
}
