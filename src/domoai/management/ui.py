"""Ephemeral, accessible Spanish client of the existing management API."""

# Embedded HTML/JS includes natural-language paragraphs longer than Python lines.
# ruff: noqa: E501
from __future__ import annotations

from starlette.responses import HTMLResponse

_PAGE = r"""<!doctype html>
<html lang="es-ES">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DomoAI — Puesta en marcha</title>
<style>
* { box-sizing: border-box; }
body { font: 1rem/1.5 system-ui, sans-serif; margin: auto; max-width: 48rem; padding: 1rem; color: #172333; background: #f7f9fc; }
h1, h2, h3 { line-height: 1.2; }
section, .card { background: white; border: 1px solid #bac7d8; border-radius: .5rem; padding: 1rem; margin: 1rem 0; min-width: 0; }
input, select, button { font: inherit; max-width: 100%; padding: .6rem; margin: .3rem 0; }
input, select { display: block; width: 100%; }
button { cursor: pointer; white-space: normal; border: 1px solid #52677e; border-radius: .3rem; background: #edf3fc; color: #172333; }
button + button { margin-left: .3rem; }
button:disabled { opacity: .55; cursor: default; }
:focus-visible { outline: 3px solid #075bc4; outline-offset: 3px; }
label { display: block; margin: .6rem 0; }
p, output, .card { overflow-wrap: anywhere; }
[hidden] { display: none !important; }
#notice { display: block; min-height: 1.5rem; }
.error { color: #9b1520; }
.help { font-size: .9rem; color: #44566d; }
</style>
</head>
<body><main>
<h1>DomoAI</h1>
<output id="notice" aria-live="polite" aria-atomic="true"></output>
<section id="login">
<h2>Acceso de administración</h2>
<form id="login-form">
<label for="management-token">Credencial de administración</label>
<input id="management-token" type="password" autocomplete="off" required>
<p class="help">Usa la credencial existente. Solo se conserva durante esta sesión de la página.</p>
<button id="login-button" type="submit">Acceder</button>
</form>
</section>
<div id="app" hidden>
<button id="logout" type="button">Cerrar sesión</button>
<section id="first-install"><h2>Comprobación inicial</h2><div id="checks"></div><p id="access-mode"></p><p id="recovery"></p></section>
<section><h2>Perfiles guardados</h2><p>Selecciona expresamente el perfil que quieres continuar.</p><div id="profiles"></div></section>
<section>
<h2>Conectar un sistema</h2><p id="catalog-status"></p><div id="catalog"></div>
<div id="actions" hidden><button id="discover" type="button">Buscar sistemas</button><button id="manual" type="button">Configurar manualmente</button></div>
<button id="cancel-setup" type="button" hidden>Cancelar configuración</button>
<div id="candidates"></div>
<form id="setup" hidden><h3 id="setup-title">Configuración</h3><div id="setup-fields"></div>
<button id="validate" type="submit">Validar conexión</button><button id="create" type="button" disabled>Crear sin activar</button>
</form>
</section>
<section><h2>Dispositivos y primera lectura</h2>
<button id="inventory" type="button">Actualizar inventario</button><p id="inventory-status"></p><div id="devices"></div>
<button id="more-devices" type="button" hidden>Mostrar más dispositivos</button>
<p id="control-status">Control: este recorrido no concede permisos ni envía órdenes.</p>
<p id="hardware-status">Hardware: no verificado.</p>
<div id="reading" aria-live="polite" aria-atomic="true"></div></section>
<section id="oauth-section"><h2>OAuth de Home Assistant</h2>
<p>Opción avanzada: autentica DomoAI ante Home Assistant. No sustituye tu acceso de administración.</p>
<form id="oauth-discover-form">
<label>URL base de Home Assistant para OAuth<input name="base_url" type="url" required></label>
<button type="submit">Descubrir OAuth</button></form>
<form id="oauth-start-form">
<label>Identificador del cliente OAuth<input name="client_id" required></label>
<label>URL de retorno OAuth<input name="redirect_uri" type="url" required></label>
<details><summary>Configuración OAuth manual</summary>
<label>URL de autorización<input name="authorization_url" type="url"></label>
<label>URL de token<input name="token_url" type="url"></label>
<label>URL de revocación<input name="revoke_url" type="url"></label>
<label>Ámbitos OAuth<input name="scopes" value="read config"></label></details>
<button type="submit">Iniciar OAuth</button></form>
<output id="oauth-result" aria-live="polite"></output></section>
<section><h2>Cliente de IA</h2><p id="client-guide"></p></section>
</div>
<script id="browser-evidence" type="application/json">{}</script>
<script>
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const state = {
    token: null, epoch: 0, flow: 0, view: 0, controllers: new Map(),
    provider: null, session: null, identity: null, manual: false,
    profiles: [], profile: null, canManage: false, canActivate: false,
    validated: false, formRevision: 0, busy: false, cursor: null
  };
  const endpointKeys = new Set(['url', 'host', 'port', 'tls', 'endpoint', 'base_url']);
  const errors = {
    forbidden: 'No tienes permiso para esta operación.',
    permission_denied: 'No tienes permiso para esta operación.',
    not_found: 'La sesión o el perfil ya no está disponible. Vuelve a buscar sistemas.',
    onboarding_session_expired: 'La configuración ha caducado. Vuelve a buscar sistemas.',
    session_expired: 'La configuración ha caducado. Vuelve a buscar sistemas.',
    connection_recovery_pending: 'Recuperación pendiente. Conserva este perfil y solicita una reparación.',
    connection_persistence_pending: 'La confirmación sigue pendiente. Consulta los perfiles antes de reintentar.',
    invalid_request: 'Revisa los campos de configuración.',
    rate_limited: 'Demasiadas solicitudes. Espera antes de reintentar.'
  };
  const expired = new Set(['not_found', 'onboarding_session_expired', 'session_expired', 'session_closed', 'invalid_session']);
  const stale = () => new DOMException('Operación sustituida.', 'AbortError');
  const stamp = () => ({ epoch: state.epoch, flow: state.flow });
  const current = (mark, setup = false) => mark.epoch === state.epoch && (!setup || mark.flow === state.flow);
  const assertCurrent = (mark, setup = false) => { if (!current(mark, setup)) throw stale(); };
  const empty = id => $(id).replaceChildren();
  const say = (message, error = false) => {
    $('notice').textContent = message;
    $('notice').className = error ? 'error' : '';
  };
  function button(label, action) {
    const element = document.createElement('button');
    element.type = 'button';
    element.textContent = label;
    element.addEventListener('click', action);
    return element;
  }
  function abort(group) {
    for (const [controller, owner] of state.controllers) {
      if (!group || owner === group) controller.abort();
    }
  }
  function clearReading() {
    state.view++;
    abort('read');
    empty('reading');
    $('browser-evidence').textContent = '{}';
  }
  function forgetSecrets() {
    document.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
  }
  function resetSetup() {
    const session = state.session;
    state.flow++;
    abort('setup');
    state.session = null;
    state.identity = null;
    state.manual = false;
    state.validated = false;
    state.busy = false;
    forgetSecrets();
    empty('setup-fields');
    empty('candidates');
    $('setup').hidden = true;
    $('cancel-setup').hidden = true;
    $('create').disabled = true;
    return session;
  }
  function clearSession(message = 'Sesión cerrada.') {
    state.epoch++;
    abort();
    resetSetup();
    clearReading();
    state.token = null;
    state.provider = null;
    state.profiles = [];
    state.profile = null;
    state.canManage = false;
    state.canActivate = false;
    state.cursor = null;
    ['checks', 'access-mode', 'recovery', 'profiles', 'catalog', 'catalog-status',
      'inventory-status', 'devices', 'client-guide', 'oauth-result'].forEach(empty);
    $('oauth-discover-form').reset(); $('oauth-start-form').reset();
    $('app').hidden = true;
    $('login').hidden = false;
    $('actions').hidden = true;
    $('more-devices').hidden = true;
    $('login-button').disabled = false;
    $('inventory').disabled = false; $('more-devices').disabled = false;
    say(message);
    $('management-token').focus();
  }
  async function request(path, method = 'GET', body, group = 'session') {
    const target = new URL(path, location.origin);
    if (target.origin !== location.origin || !state.token) throw stale();
    const token = state.token;
    const mark = stamp();
    const controller = new AbortController();
    state.controllers.set(controller, group);
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(target.href, {
        method, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        headers: { Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined, signal: controller.signal
      });
      // Old 401 responses cannot revoke a later principal.
      assertCurrent(mark, group === 'setup');
      if (token !== state.token) throw stale();
      if (response.status === 401) {
        clearSession('La credencial no es válida o ha caducado. Accede de nuevo.');
        throw stale();
      }
      const data = await response.json();
      assertCurrent(mark, group === 'setup');
      if (!response.ok) {
        const error = new Error(errors[data.error] || (response.status === 403
          ? 'No tienes permiso para esta operación.' : 'No se pudo completar la operación. Puedes reintentar.'));
        error.code = data.error;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
      state.controllers.delete(controller);
    }
  }
  async function cancel(session) {
    if (!session || !state.token) return;
    try { await request('/api/v1/connections/setup', 'POST', { action: 'cancel', session_id: session }); }
    catch (_) { /* Local cancellation is final; server TTL also closes abandoned sessions. */ }
  }
  function report(error, mark, setup = false) {
    if (!current(mark, setup)) return;
    say(error.name === 'AbortError' ? 'La solicitud ha terminado sin confirmación. Puedes reintentar.' : error.message, true);
  }
  function descriptors() {
    return (state.provider?.steps || []).flatMap(step => step.fields || []);
  }
  function updateCreateButton() {
    $('create').disabled = state.busy || $('setup').hidden ||
      (state.manual ? !$('setup').checkValidity() : !state.validated);
  }
  function renderFields(endpoint = {}) {
    empty('setup-fields');
    for (const field of descriptors()) {
      const label = document.createElement('label');
      label.htmlFor = 'setup-' + field.key;
      label.textContent = ({ 'Home Assistant URL': 'URL de Home Assistant', 'Access token': 'Token de acceso' })[field.label] || field.label;
      const input = document.createElement(field.input_type === 'select' ? 'select' : 'input');
      input.id = label.htmlFor;
      input.name = field.key;
      input.required = field.required;
      if (input.tagName === 'INPUT') input.type = field.secret ? 'password' : field.input_type;
      if (field.secret) input.autocomplete = 'off';
      for (const option of field.options || []) {
        const item = document.createElement('option');
        item.value = option; item.textContent = option; input.append(item);
      }
      if (!field.secret && endpoint[field.key] !== undefined) input.value = endpoint[field.key];
      input.addEventListener('input', () => {
        state.formRevision++;
        state.validated = false;
        updateCreateButton();
      });
      label.append(input);
      $('setup-fields').append(label);
      if (field.help_text) {
        const help = document.createElement('span');
        help.id = input.id + '-help'; help.className = 'help'; help.textContent = ({ 'The base URL of the Home Assistant instance.': 'URL base de tu instalación de Home Assistant.', 'Stored in the configured secret store.': 'Se guardará en el almacén de secretos configurado.' })[field.help_text] || field.help_text;
        input.setAttribute('aria-describedby', help.id); $('setup-fields').append(help);
      }
    }
    $('setup').hidden = false;
    $('cancel-setup').hidden = false;
    $('validate').hidden = state.manual;
    $('validate').disabled = false;
    updateCreateButton();
    $('setup-fields').querySelector('input,select')?.focus();
  }
  function draft() {
    const endpoint = {}, options = {};
    let credential, mapping_ref;
    for (const field of descriptors()) {
      const input = $('setup-' + field.key);
      if (!input.value) continue;
      const value = field.input_type === 'number' ? Number(input.value) : input.value;
      if (field.secret) credential = input.value;
      else if (field.key === 'mapping_ref') mapping_ref = value;
      else if (endpointKeys.has(field.key)) endpoint[field.key] = value;
      else options[field.key] = value;
    }
    return { endpoint, options, credential, mapping_ref };
  }
  function canonical(value) {
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => canonical(key) + ':' + canonical(value[key])).join(',') + '}';
    return JSON.stringify(value).replace(/[\u007f-\uffff]/g, char => '\\u' + char.charCodeAt(0).toString(16).padStart(4, '0'));
  }
  async function manualIdentity(provider, values) {
    const bytes = new TextEncoder().encode(canonical({ connector_id: provider, endpoint: values.endpoint, options: values.options }));
    const hash = await crypto.subtle.digest('SHA-256', bytes);
    return provider + ':manual:' + Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, '0')).join('').slice(0, 16);
  }
  function ownMatch(identity) {
    if (!identity) return null;
    const matches = state.profiles.filter(profile => profile.connector_id === identity.connector && profile.unique_id === identity.key);
    return matches.length === 1 ? matches[0] : null;
  }
  async function reconcile(id, identity, mark = stamp(), setup = false) {
    const data = await request('/api/v1/connections');
    assertCurrent(mark, setup);
    state.profiles = data.connections || [];
    state.profile = id ? state.profiles.find(profile => profile.id === id) || null : ownMatch(identity);
    renderProfiles();
    return state.profile;
  }
  function renderProfiles() {
    empty('profiles');
    if (!state.profiles.length) { $('profiles').textContent = 'No hay perfiles guardados.'; return; }
    for (const profile of state.profiles) {
      const card = document.createElement('div');
      card.className = 'card';
      card.dataset.profileId = profile.id;
      const title = document.createElement('p');
      title.textContent = profile.connector_id + ' · ' + profile.unique_id + ' · ' + (profile.enabled ? 'Activación guardada' : 'Sin activar');
      card.append(title);
      const selected = state.profile?.id === profile.id;
      const select = button(selected ? 'Perfil seleccionado' : 'Seleccionar perfil', () => {
        clearReading(); empty('devices'); empty('inventory-status'); state.cursor = null; $('more-devices').hidden = true;
        state.profile = profile; renderProfiles(); say('Perfil seleccionado.');
        $('profiles').querySelector('[aria-pressed="true"]')?.focus();
      });
      select.setAttribute('aria-pressed', String(selected)); card.append(select);
      if (selected && state.canManage) {
        const activate = button('Activar conexión', () => operate(profile, 'activate'));
        activate.disabled = !state.canActivate || profile.enabled;
        card.append(activate, button('Reparar conexión', () => operate(profile, 'repair')),
          button('Consultar diagnóstico', () => diagnose(profile)));
        const form = document.createElement('form');
        const label = document.createElement('label');
        label.textContent = 'Nueva credencial del proveedor';
        const credential = document.createElement('input');
        credential.type = 'password'; credential.autocomplete = 'off'; credential.required = true;
        label.append(credential); form.append(label);
        const submit = document.createElement('button');
        submit.type = 'submit'; submit.textContent = 'Actualizar credencial'; form.append(submit);
        form.addEventListener('submit', event => {
          event.preventDefault(); const value = credential.value; credential.value = '';
          if (value) void operate(profile, 'reauth', { credential: value });
        });
        card.append(form);
      }
      $('profiles').append(card);
    }
  }
  async function diagnose(profile) {
    const mark = stamp();
    try {
      const result = await request('/api/v1/connections/' + encodeURIComponent(profile.id) + '/diagnostics');
      assertCurrent(mark);
      if (state.profile?.id !== profile.id) return;
      const diagnostics = result.diagnostics || [];
      say(diagnostics.length ? 'Diagnóstico: ' + diagnostics.map(item => errors[item.code] || 'El proveedor requiere revisión.').join(' ') : 'Diagnóstico: sin incidencias comunicadas.');
    } catch (error) { report(error, mark); }
  }
  async function operate(profile, operation, body) {
    const mark = stamp();
    $('profiles').querySelectorAll('button').forEach(element => { element.disabled = true; });
    try {
      const result = await request('/api/v1/connections/' + encodeURIComponent(profile.id) + '/' + operation, 'POST', body);
      assertCurrent(mark);
      await reconcile(profile.id);
      assertCurrent(mark);
      const success = result.success !== false && result.valid !== false;
      say(success ? (operation === 'activate' ? 'Conexión activada.' : operation === 'reauth'
        ? 'Credencial actualizada para el mismo perfil.' : 'Reparación solicitada para el mismo perfil.')
        : 'La conexión sigue pendiente. Revisa la credencial o solicita una reparación.', !success);
      $('profiles').querySelector('[aria-pressed="true"]')?.focus();
    } catch (error) {
      if (current(mark) && state.token) {
        await reconcile(profile.id).catch(() => {});
        report(error, mark);
      }
    }
  }
  async function chooseProvider(provider) {
    const previous = resetSetup();
    state.provider = provider;
    $('actions').hidden = !state.canManage;
    $('discover').hidden = !provider.supports_discovery;
    $('manual').hidden = !provider.supports_manual_setup;
    $('discover').disabled = false;
    $('manual').disabled = false;
    say('Proveedor seleccionado: ' + provider.display_name);
    (provider.supports_discovery ? $('discover') : $('manual')).focus();
    await cancel(previous);
  }
  async function load() {
    const mark = stamp();
    const status = await request('/api/v1/onboarding/status');
    assertCurrent(mark);
    state.canManage = status.can_manage_connections === true;
    state.canActivate = state.canManage && (status.checks || []).filter(check => ['service', 'lifecycle', 'ownership'].includes(check.code)).every(check => check.status === 'passed');
    const names = { service: 'Servicio', lifecycle: 'Adquisición de datos', ownership: 'Responsabilidad del runtime', connectors: 'Conectores' };
    const labels = { passed: 'preparado', failed: 'requiere atención', pending: 'pendiente' };
    for (const check of status.checks || []) {
      const item = document.createElement('p'); item.textContent = names[check.code] + ': ' + labels[check.status]; $('checks').append(item);
    }
    $('access-mode').textContent = (status.auth_mode === 'local_loopback' ? 'Acceso local confirmado por el servidor. ' : 'Credencial comprobada por el servidor. ') + (state.canManage ? 'Administración disponible.' : 'Acceso de solo lectura.');
    const recovery = (status.recovery_diagnostics || []).filter(code => ['connection_recovery_pending', 'connection_persistence_pending'].includes(code)).slice(0, 2);
    $('recovery').textContent = recovery.length ? 'Recuperación pendiente. Conserva los perfiles existentes y usa su reparación explícita.' : '';
    const guide = status.client_connection || {};
    $('client-guide').textContent = (guide.status === 'prepared' ? 'Configuración preparada; comprueba la conexión en tu cliente. URL: ' + guide.url + ' · Transporte: ' + guide.transport + '. ' : 'Configuración del cliente pendiente. ') + 'Usa una credencial de solo lectura ya provisionada por tu administrador, distinta de la credencial de administración. No necesitas otro modelo de IA para completar el servidor.';
    const catalog = await request('/api/v1/connection-providers');
    assertCurrent(mark);
    const providers = [...(catalog.providers || [])].sort((a, b) => (a.connector_id === 'home_assistant' ? -1 : b.connector_id === 'home_assistant' ? 1 : a.display_name.localeCompare(b.display_name, 'es-ES')));
    for (const provider of providers) {
      const item = button(provider.display_name, () => chooseProvider(provider)); item.disabled = !state.canManage; $('catalog').append(item);
    }
    $('catalog-status').textContent = providers.length ? 'Elige un proveedor para continuar.' : 'No hay proveedores disponibles. La conexión queda pendiente.';
    await reconcile();
    assertCurrent(mark);
    $('oauth-section').hidden = !state.canManage || !providers.some(provider => provider.connector_id === 'home_assistant');
    $('app').hidden = false; $('login').hidden = true; $('logout').focus();
    say('Acceso confirmado.');
  }
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const token = $('management-token').value; $('management-token').value = '';
    if (!token) { say('Introduce la credencial de administración.', true); return; }
    clearSession('Comprobando acceso…');
    state.token = token; const mark = stamp(); $('login-button').disabled = true;
    try { await load(); }
    catch (error) { if (current(mark)) { clearSession(); say(error.message, true); } }
  });
  $('logout').addEventListener('click', () => clearSession());
  $('cancel-setup').addEventListener('click', async () => {
    const previous = resetSetup(); say('Configuración cancelada.'); $('discover').focus(); await cancel(previous);
  });
  $('manual').addEventListener('click', async () => {
    const previous = resetSetup(); state.manual = true; renderFields();
    say('Completa los campos del proveedor. La creación valida la conexión antes de guardarla sin activar.');
    await cancel(previous);
  });
  $('discover').addEventListener('click', async () => {
    const previous = resetSetup(); const mark = stamp(); const provider = state.provider;
    $('discover').disabled = true; $('cancel-setup').hidden = false;
    try {
      await cancel(previous); assertCurrent(mark, true);
      const result = await request('/api/v1/connections/setup', 'POST', { action: 'start', connector_id: provider.connector_id }, 'setup');
      assertCurrent(mark, true); state.session = result.session_id;
      for (const candidate of result.candidates || []) {
        const item = button('Seleccionar ' + candidate.display_name, async () => {
          state.flow++; abort('setup'); state.validated = false; forgetSecrets();
          $('create').disabled = true; $('setup').hidden = true;
          const selected = stamp();
          $('candidates').querySelectorAll('button').forEach(element => { element.disabled = true; });
          try {
            const response = await request('/api/v1/connections/setup', 'POST', { action: 'select', session_id: state.session, candidate_id: candidate.candidate_id }, 'setup');
            assertCurrent(selected, true);
            state.identity = { connector: provider.connector_id, key: response.candidate.candidate_id };
            renderFields(response.candidate.endpoint); say('Sistema seleccionado.');
          } catch (error) {
            if (current(selected, true) && (expired.has(error.code) || error.code === 'invalid_request')) {
              const session = resetSetup(); void cancel(session);
              say('La sesión no admite este paso o ha caducado. Vuelve a buscar sistemas.', true); $('discover').focus();
            } else report(error, selected, true);
          }
          finally { if (current(selected, true)) $('candidates').querySelectorAll('button').forEach(element => { element.disabled = false; }); }
        });
        $('candidates').append(item);
      }
      if (!(result.candidates || []).length) {
        say('No se encontraron sistemas. Puedes configurar manualmente.'); $('manual').focus();
      } else { say('Selecciona el sistema encontrado.'); $('candidates').querySelector('button')?.focus(); }
    } catch (error) { report(error, mark, true); }
    finally { if (current(mark, true)) $('discover').disabled = false; }
  });
  $('setup').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.manual || state.busy || !state.session) return;
    state.busy = true; state.validated = false; $('create').disabled = true; $('validate').disabled = true;
    const mark = stamp(); const values = draft(); forgetSecrets();
    const revision = state.formRevision;
    say('Validando conexión…');
    try {
      await request('/api/v1/connections/setup', 'POST', { action: 'setup', session_id: state.session, ...values }, 'setup');
      assertCurrent(mark, true);
      if (revision !== state.formRevision) return;
      const result = await request('/api/v1/connections/setup', 'POST', { action: 'validate', session_id: state.session }, 'setup');
      assertCurrent(mark, true);
      if (revision !== state.formRevision) return;
      state.validated = result.valid === true;
      $('create').disabled = !state.validated;
      say(state.validated ? 'Conexión validada.' : 'No se ha validado. Reintentar con una credencial nueva.', !state.validated);
      (state.validated ? $('create') : $('setup-fields').querySelector('input[type="password"]'))?.focus();
    } catch (error) {
      if (current(mark, true) && revision === state.formRevision) {
        if (expired.has(error.code) || error.code === 'invalid_request') {
          const session = resetSetup(); void cancel(session);
          say('La sesión no admite este paso o ha caducado. Vuelve a buscar sistemas.', true); $('discover').focus();
        }
        else { say('No se ha validado. Reintentar con una credencial nueva.', true); $('setup-fields').querySelector('input[type="password"]')?.focus(); }
      }
    } finally {
      if (current(mark, true)) {
        state.busy = false; $('validate').disabled = false;
        updateCreateButton();
        if (revision !== state.formRevision) say('La configuración ha cambiado. Valida los datos actuales.');
      }
    }
  });
  $('create').addEventListener('click', async () => {
    if (state.busy || (!state.manual && (!state.session || !state.validated))) return;
    if (state.manual && !$('setup').reportValidity()) return;
    state.busy = true; $('create').disabled = true; $('validate').disabled = true;
    const mark = stamp(); const provider = state.provider.connector_id;
    const values = state.manual ? draft() : null; forgetSecrets();
    let identity = state.identity;
    try {
      if (state.manual) {
        identity = { connector: provider, key: await manualIdentity(provider, values) };
        assertCurrent(mark, true); state.identity = identity;
      }
      const result = state.manual
        ? await request('/api/v1/connections', 'POST', { connector_id: provider, unique_id: identity.key, ...values, activate: false }, 'setup')
        : await request('/api/v1/connections/setup', 'POST', { action: 'create', session_id: state.session, activate: false }, 'setup');
      assertCurrent(mark, true);
      await reconcile(result.connection.id, identity, mark, true); assertCurrent(mark, true);
      const previous = resetSetup(); void cancel(previous);
      say('Conexión creada sin activar.'); $('profiles').querySelector('[aria-pressed="true"]')?.focus();
    } catch (error) {
      if (!current(mark, true)) return;
      // A lost response is not proof of failure. Match only our exact scoped identity.
      try {
        const existing = await reconcile(undefined, identity, mark, true); assertCurrent(mark, true);
        if (existing) {
          const previous = resetSetup(); void cancel(previous);
          say('Perfil existente recuperado. Continúa con el mismo perfil sin crear otra conexión.');
          $('profiles').querySelector('[aria-pressed="true"]')?.focus(); return;
        }
      } catch (_) { if (!current(mark, true)) return; }
      state.validated = false;
      if (expired.has(error.code) || (!state.manual && error.code === 'invalid_request')) {
        const previous = resetSetup(); void cancel(previous); say(errors[error.code] || 'Configuración caducada. Vuelve a buscar sistemas.', true);
      } else {
        say('No se confirmó la creación. Reintentar con una credencial nueva; se conservará la identidad.', true);
        $('setup-fields').querySelector('input[type="password"]')?.focus();
      }
    } finally {
      if (current(mark, true)) { state.busy = false; $('validate').disabled = false; updateCreateButton(); }
    }
  });
  async function readDevice(device, capability) {
    clearReading();
    const mark = stamp(); const view = state.view; const profile = state.profile;
    $('reading').textContent = 'Primera lectura pendiente. Consultando la fuente…';
    try {
      const query = new URLSearchParams({ capability: capability.name });
      if (profile) query.set('connection_id', profile.id);
      const result = await request('/api/v1/onboarding/state/' + encodeURIComponent(device.id) + '?' + query, 'GET', undefined, 'read');
      assertCurrent(mark); if (view !== state.view) return;
      const snapshot = result.states?.find(item => item.capability === capability.name);
      if (result.read_status !== 'verified' || !snapshot || !snapshot.observed_at || !snapshot.source_ref) {
        const reasons = { stale: 'datos antiguos', invalid: 'valor inválido', unavailable: 'no disponible', source_unavailable: 'fuente retirada', state_missing: 'sin datos', connection_mismatch: 'perfil distinto', source_mismatch: 'fuente distinta', capability_unavailable: 'capacidad no disponible' };
        $('reading').textContent = 'Primera lectura pendiente: ' + ((result.diagnostics || []).map(code => reasons[code] || 'sin confirmar').join(', ') || 'sin datos actuales') + '. Puedes volver a consultar.';
        return;
      }
      $('reading').textContent = 'Lectura verificada: ' + snapshot.value + ' ' + (snapshot.unit || '') + ' · ' + snapshot.observed_at + ' · fuente: ' + snapshot.source_ref.adapter_id + '.';
      $('browser-evidence').textContent = JSON.stringify({ profile_id: profile?.id, device_id: device.id, capability: capability.name, value: snapshot.value, unit: snapshot.unit, timestamp: snapshot.observed_at, source: snapshot.source_ref.adapter_id });
    } catch (error) {
      if (current(mark) && view === state.view) { $('reading').textContent = 'Primera lectura pendiente. Puedes volver a consultar.'; report(error, mark); }
    }
  }
  async function inventory(more = false) {
    clearReading(); const mark = stamp(); const view = state.view;
    $('inventory').disabled = true; $('more-devices').disabled = true;
    if (!more) { empty('devices'); state.cursor = null; $('more-devices').hidden = true; }
    try {
      const query = state.cursor && more ? '?' + new URLSearchParams({ cursor: state.cursor }) : '';
      const result = await request('/api/v1/onboarding/inventory' + query, 'GET', undefined, 'read');
      assertCurrent(mark); if (view !== state.view) return;
      $('inventory-status').textContent = 'Inventario observado: ' + result.observed_at + '. El inventario no confirma una lectura.';
      state.cursor = result.next_cursor; $('more-devices').hidden = !state.cursor;
      if (!(result.devices || []).length && !more) {
        $('devices').textContent = 'Sin dispositivos encontrados. Primera lectura pendiente.'; return;
      }
      for (const device of result.devices || []) {
        const card = document.createElement('div'); card.className = 'card';
        const name = document.createElement('h3'); name.textContent = device.name; card.append(name);
        for (const capability of device.capabilities || []) {
          if (!capability.readable) continue;
          const row = result.coverage_evidence?.rows?.find(item => item.device_id === device.id && item.capability === capability.name);
          const detail = document.createElement('p');
          detail.textContent = capability.name + ' · Ruta: ' + (row?.status === 'active' ? 'disponible' : 'sin confirmar') + ' · Hardware: no verificado.';
          card.append(detail, button('Ver lectura de ' + capability.name, () => readDevice(device, capability)));
        }
        $('devices').append(card);
      }
    } catch (error) { if (current(mark) && view === state.view) report(error, mark); }
    finally { if (current(mark)) { $('inventory').disabled = false; $('more-devices').disabled = false; } }
  }
  $('inventory').addEventListener('click', () => inventory());
  $('more-devices').addEventListener('click', () => inventory(true));
  $('oauth-discover-form').addEventListener('submit', async event => {
    event.preventDefault(); const mark = stamp(); empty('oauth-result');
    try {
      const result = await request('/api/v1/home-assistant/oauth/discover', 'POST', { base_url: new FormData(event.currentTarget).get('base_url') });
      assertCurrent(mark);
      $('oauth-result').textContent = result.metadata ? 'Endpoints OAuth encontrados. Puedes iniciar la autorización.' : 'Completa la configuración OAuth manual.';
    } catch (error) { report(error, mark); }
  });
  $('oauth-start-form').addEventListener('submit', async event => {
    event.preventDefault(); const mark = stamp(); empty('oauth-result');
    const data = new FormData(event.currentTarget);
    const body = { base_url: new FormData($('oauth-discover-form')).get('base_url'), client_id: data.get('client_id'), redirect_uri: data.get('redirect_uri') };
    if (data.get('authorization_url') && data.get('token_url')) body.config = {
      authorization_url: data.get('authorization_url'), token_url: data.get('token_url'),
      revoke_url: data.get('revoke_url') || null, client_id: body.client_id,
      redirect_uri: body.redirect_uri, scopes: String(data.get('scopes') || '').split(/\s+/).filter(Boolean)
    };
    try {
      const result = await request('/api/v1/home-assistant/oauth/start', 'POST', body);
      assertCurrent(mark);
      const url = new URL(result.authorization_url);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('URL OAuth no disponible.');
      const link = document.createElement('a'); link.href = url.href;
      link.rel = 'noopener noreferrer'; link.referrerPolicy = 'no-referrer'; link.target = '_blank';
      link.textContent = 'Continuar la autorización en Home Assistant'; $('oauth-result').append(link);
      link.focus();
    } catch (error) { report(error, mark); }
  });
  window.addEventListener('pagehide', () => clearSession());
})();
</script>
</main></body></html>
"""


def first_install_page() -> HTMLResponse:
    """Return the same public shell for every principal; API calls load private data."""
    return HTMLResponse(_PAGE, media_type="text/html")


__all__ = ["first_install_page"]
