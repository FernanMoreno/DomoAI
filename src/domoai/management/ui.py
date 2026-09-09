"""Small catalog-driven server-rendered connection setup view."""

from __future__ import annotations

from starlette.responses import HTMLResponse


def first_install_page() -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>DomoAI connections</title></head>
<body><main>
<h1>Connection control plane</h1>
<section id="first-install" data-state="empty"><h2>First install</h2>
<p>No physical provider is required to start. Discover a system or add one manually.</p>
<form method="post" action="/api/v1/connections/discovery" id="discover-form">
<label>Connector <select name="connector_id" id="connector-id" required></select></label>
<button type="submit">Found systems</button></form>
<a href="#manual-add">Manual add</a></section>
<section id="provider-catalog"><h2>Available connectors</h2>
<p id="catalog-status">Loading connectors…</p>
<div id="provider-cards"></div></section>
<section id="home-assistant-oauth"><h2>Home Assistant OAuth</h2>
<p>Enter the Home Assistant base URL. Standard OAuth/OIDC endpoints are
discovered automatically.</p>
<form id="oauth-discover-form" action="/api/v1/home-assistant/oauth/discover">
<label>Base URL <input name="base_url" type="url" required></label>
<button>Discover OAuth</button></form>
<form id="oauth-start-form" action="/api/v1/home-assistant/oauth/start">
<label>Client ID <input name="client_id" required></label>
<label>Redirect URI <input name="redirect_uri" type="url" required></label>
<details><summary>Manual OAuth fallback</summary>
<label>Authorization URL <input name="authorization_url" type="url"></label>
<label>Token URL <input name="token_url" type="url"></label>
<label>Revoke URL <input name="revoke_url" type="url"></label>
<label>Scopes <input name="scopes" value="read config"></label></details>
<button>Start OAuth</button></form>
<output id="oauth-result" aria-live="polite"></output></section>
<section id="manual-add"><h2>Manual add</h2>
<form method="post" action="/api/v1/connections" id="manual-add-form">
<input name="connector_id" id="manual-connector-id" type="hidden">
<div id="setup-fields"></div>
<label>Endpoint <input name="endpoint" id="legacy-endpoint" type="url"></label>
<label>Credentials <input name="credential" id="legacy-credential"
type="password" autocomplete="off"></label>
<button>Add connection</button></form></section>
<nav aria-label="connection states"><span>existing</span><span>connected</span>
<span>degraded</span><span>reauth</span><span>diagnostics</span><span>remove</span>
<span>advanced settings</span><span>mapping</span><span>success</span></nav>
<output id="management-result" aria-live="polite"></output>
<script>
const catalogUrl = "/api/v1/connection-providers";
const endpointKeys = new Set(["url", "host", "port", "tls", "endpoint", "base_url"]);
let providerCatalog = [];
let selectedProvider = null;

const jsonForm = (form, transform) => form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = document.getElementById("management-result");
  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(transform(new FormData(form)))
    });
    result.textContent = response.ok ? "success" : "operation failed";
  } catch (_) { result.textContent = "operation failed"; }
});

function renderProviderCatalog(providers) {
  providerCatalog = providers;
  const connector = document.getElementById("connector-id");
  connector.replaceChildren();
  for (const provider of providers) {
    const option = document.createElement("option");
    option.value = provider.connector_id;
    option.textContent = provider.display_name;
    connector.appendChild(option);
  }
  connector.addEventListener("change", () => selectProvider(connector.value));
  if (providers.length) selectProvider(providers[0].connector_id);
  document.getElementById("catalog-status").textContent = providers.length
    ? "Select a connector and complete its setup fields."
    : "No connectors are installed.";
}

function selectProvider(connectorId) {
  selectedProvider = providerCatalog.find((provider) => provider.connector_id === connectorId);
  document.getElementById("manual-connector-id").value = connectorId || "";
  const container = document.getElementById("setup-fields");
  container.replaceChildren();
  const setup = selectedProvider
    ? {fields: selectedProvider.steps.flatMap((step) => step.fields)}
    : {fields: []};
  for (const field of setup.fields) {
      const label = document.createElement("label");
      label.textContent = field.label;
      const input = field.input_type === "select"
        ? document.createElement("select")
        : document.createElement("input");
      input.name = field.key;
      if (field.input_type !== "select") input.type = field.input_type;
      input.required = field.required;
      input.title = field.help_text;
      if (field.input_type === "select") {
        for (const optionValue of field.options) {
          const option = document.createElement("option");
          option.value = optionValue;
          option.textContent = optionValue;
          input.appendChild(option);
        }
      }
      label.appendChild(input);
      container.appendChild(label);
  }
}

jsonForm(document.getElementById("discover-form"), (data) => ({
  connector_id: data.get("connector_id")
}));
jsonForm(document.getElementById("manual-add-form"), (data) => {
  const endpoint = {};
  const options = {};
  let credential;
  const secretKeys = new Set(
    (selectedProvider?.steps || []).flatMap((step) => step.fields)
      .filter((field) => field.secret).map((field) => field.key)
  );
  let mapping_ref;
  for (const [key, value] of data.entries()) {
    if (["connector_id", "endpoint", "credential"].includes(key) || !value) continue;
    if (secretKeys.has(key) || ["token", "password", "secret"].includes(key)) credential = value;
    else if (key === "mapping_ref") mapping_ref = value;
    else if (endpointKeys.has(key)) {
      endpoint[key] = key === "port" ? Number(value) : key === "tls" ? value === "true" : value;
    }
    else options[key] = value;
  }
  if (data.get("endpoint")) endpoint.url = data.get("endpoint");
  return {connector_id: data.get("connector_id"), endpoint, options, credential, mapping_ref};
});

fetch(catalogUrl).then((response) => response.json()).then((data) => {
  renderProviderCatalog(data.providers || []);
}).catch(() => {
  document.getElementById("catalog-status").textContent = "Connector catalog unavailable.";
});

let oauthMetadata = null;
document.getElementById("oauth-discover-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const result = document.getElementById("oauth-result");
  try {
    const response = await fetch(form.action, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({base_url: new FormData(form).get("base_url")})
    });
    const data = await response.json();
    oauthMetadata = data.metadata || null;
    result.textContent = oauthMetadata ? "OAuth endpoints found" : "Manual OAuth fields required";
  } catch (_) { result.textContent = "OAuth discovery failed"; }
});
jsonForm(document.getElementById("oauth-start-form"), (data) => {
  const body = {
    base_url: document.querySelector("#oauth-discover-form [name=base_url]").value,
    client_id: data.get("client_id"), redirect_uri: data.get("redirect_uri")
  };
  if (data.get("authorization_url") && data.get("token_url")) {
    body.config = {
      authorization_url: data.get("authorization_url"), token_url: data.get("token_url"),
      revoke_url: data.get("revoke_url") || null, client_id: data.get("client_id"),
      redirect_uri: data.get("redirect_uri"), scopes: String(data.get("scopes") || "").split(
        /\\s+/
      ).filter(Boolean)
    };
  }
  return body;
});
</script>
</main></body></html>""",
        media_type="text/html",
    )


__all__ = ["first_install_page"]
