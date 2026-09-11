/* Real product HTTP journey. Fault injection below tests browser response handling only. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const baseUrl = process.env.P01_BROWSER_BASE_URL;
const token = process.env.P01_BROWSER_TOKEN;
const readToken = process.env.P01_BROWSER_READ_TOKEN;
const evidencePath = process.env.P01_BROWSER_EVIDENCE;
const authorization = process.env.P01_BROWSER_AUTHORIZATION;
const haUrl = process.env.P01_BROWSER_HA_URL;

async function product(browser, context, page, response) {
  const scenarios = [];
  const errors = [];
  const requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    const auth = request.headers().authorization;
    if (auth) {
      assert.equal(new URL(request.url()).origin, new URL(baseUrl).origin);
      requests.push({ path: new URL(request.url()).pathname, method: request.method() });
    }
  });
  const notice = text => page.getByText(text, { exact: true }).waitFor();
  const click = name => page.getByRole('button', { name, exact: true }).click();
  async function login(credential = token) {
    await page.getByLabel('Credencial de administración', { exact: true }).fill(credential);
    await page.locator('#management-token').press('Enter');
    await notice('Acceso confirmado.');
    assert.equal(await page.locator('#management-token').inputValue(), '');
  }
  async function selectSource() {
    await click('Home Assistant');
    await click('Buscar sistemas');
    await click('Seleccionar Salón');
    await notice('Sistema seleccionado.');
  }
  async function validate(credential = 'synthetic-ha-fixture') {
    await page.getByLabel('Token de acceso', { exact: true }).fill(credential);
    await click('Validar conexión');
  }
  async function inventory() {
    await click('Actualizar inventario');
    try { await page.getByRole('button', { name: 'Ver lectura de temperature', exact: true }).waitFor(); }
    catch (_) { throw new Error('Inventory boundary: ' + await page.locator('#notice').textContent()); }
  }
  async function read() {
    await click('Ver lectura de temperature');
    await page.waitForFunction(() => document.querySelector('#reading').textContent.startsWith('Lectura verificada:'));
    return page.locator('#browser-evidence').evaluate(node => JSON.parse(node.textContent));
  }
  async function assertClean() {
    assert.equal(await page.locator('#browser-evidence').textContent(), '{}');
    for (const selector of ['#profiles', '#devices', '#reading', '#setup-fields', '#client-guide', '#checks', '#oauth-result']) {
      assert.equal(await page.locator(selector).textContent(), '');
    }
    assert.deepEqual(await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) })), { local: [], session: [] });
    assert.equal(new URL(page.url()).search, '');
    assert.equal(await page.locator('#management-token').inputValue(), '');
    assert.equal(await page.locator('#management-token').evaluate(node => document.activeElement === node), true);
  }
  assert.equal(response.status(), 200);
  assert.equal(await page.locator('html').getAttribute('lang'), 'es-ES');
  // Real keyboard access: Tab, Shift+Tab, and Enter submit the password form.
  await page.keyboard.press('Tab');
  assert.equal(await page.locator('#management-token').evaluate(node => node === document.activeElement), true);
  await page.keyboard.press('Tab');
  assert.equal(await page.locator('#login-button').evaluate(node => node === document.activeElement), true);
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.locator('#management-token').evaluate(node => getComputedStyle(node).outlineStyle), 'solid');
  await login();
  assert.equal(await page.locator('#catalog button').first().textContent(), 'Home Assistant');
  assert.ok(await page.locator('#catalog button').count() > 1);
  assert.match(await page.locator('#client-guide').textContent(), /Configuración preparada.*streamable-http.*distinta de la credencial de administración/s);
  assert.match(await page.locator('#control-status').textContent(), /no concede permisos ni envía órdenes/);
  assert.match(await page.locator('#hardware-status').textContent(), /no verificado/);

  // R04: HA itself rejects the wrong token; fields and candidate survive.
  await selectSource();
  assert.equal(await page.getByLabel('URL de Home Assistant', { exact: true }).inputValue(), haUrl);
  await validate('invalid-ha-fixture');
  await notice('No se ha validado. Reintentar con una credencial nueva.');
  assert.equal(await page.getByLabel('Token de acceso', { exact: true }).inputValue(), '');
  assert.equal(await page.getByLabel('URL de Home Assistant', { exact: true }).inputValue(), haUrl);
  assert.equal(await page.locator('#notice').getAttribute('aria-live'), 'polite');
  assert.equal(await page.getByLabel('Token de acceso', { exact: true }).evaluate(node => node === document.activeElement), true);
  await validate(); await notice('Conexión validada.');
  scenarios.push('R04');

  // R12: cancelling a validated setup cannot re-enable create or retain a secret.
  await click('Cancelar configuración');
  await notice('Configuración cancelada.');
  assert.equal(await page.locator('#setup').isVisible(), false);
  assert.equal(await page.locator('#create').isEnabled(), false);
  assert.equal(await page.locator('#setup-fields').textContent(), '');
  await selectSource(); await validate(); await notice('Conexión validada.');
  // An expired session is a server rejection, never permission to reuse a validated draft.
  await page.route('**/api/v1/connections/setup', async route => {
    if (route.request().postDataJSON().action === 'validate') return route.fulfill({ status: 400, json: { error: 'invalid_request' } });
    await route.continue();
  });
  await validate();
  await page.waitForFunction(() => document.querySelector('#setup').hidden);
  assert.equal(await page.locator('#create').isEnabled(), false);
  await page.unroute('**/api/v1/connections/setup');
  await selectSource(); await validate(); await notice('Conexión validada.');
  scenarios.push('R12');

  // R05: forward create to the real server, then lose the response after commit.
  let committed;
  let createCount = 0;
  await page.route('**/api/v1/connections/setup', async route => {
    const body = route.request().postDataJSON();
    if (body.action !== 'create') return route.continue();
    createCount++;
    const result = await route.fetch();
    assert.equal(result.status(), 201);
    committed = (await result.json()).connection;
    assert.equal(committed.enabled, false);
    await route.abort('connectionreset');
  });
  await page.locator('#create').evaluate(node => { node.click(); node.click(); });
  await notice('Perfil existente recuperado. Continúa con el mismo perfil sin crear otra conexión.');
  await page.unroute('**/api/v1/connections/setup');
  assert.equal(createCount, 1);
  assert.equal(await page.locator('[data-profile-id]').count(), 1);
  assert.equal(await page.locator('[data-profile-id]').getAttribute('data-profile-id'), committed.id);
  await click('Activar conexión'); await notice('Conexión activada.');
  await inventory();
  const evidence = await read();
  assert.equal(evidence.profile_id, committed.id);
  assert.equal(evidence.value, 21.5);
  assert.equal(evidence.unit, '°C');
  assert.ok(evidence.timestamp && evidence.source && evidence.device_id);
  assert.match(await page.locator('#devices').textContent(), /Ruta: disponible.*Hardware: no verificado/s);
  scenarios.push('R03', 'R07');

  await click('Consultar diagnóstico');
  await page.waitForFunction(() => document.querySelector('#notice').textContent.startsWith('Diagnóstico:'));

  // Repair and reauth use the UUID obtained from the real create.
  await click('Reparar conexión'); await notice('Reparación solicitada para el mismo perfil.');
  await page.getByLabel('Nueva credencial del proveedor').fill('synthetic-ha-fixture');
  await click('Actualizar credencial'); await notice('Credencial actualizada para el mismo perfil.');
  assert.equal(await page.locator('[data-profile-id]').getAttribute('data-profile-id'), committed.id);
  assert.equal(await page.getByLabel('Nueva credencial del proveedor').inputValue(), '');
  assert.ok(requests.some(item => item.path === `/api/v1/connections/${committed.id}/reauth`));
  assert.ok(requests.some(item => item.path === `/api/v1/connections/${committed.id}/repair`));

  // Reload has no stored auth, restores profiles only after login, and requires selection.
  await page.reload();
  await page.getByLabel('Credencial de administración', { exact: true }).waitFor();
  await login();
  assert.equal(await page.getByRole('button', { name: 'Activar conexión', exact: true }).count(), 0);
  await click('Seleccionar perfil');
  assert.equal(await page.locator('[data-profile-id]').getAttribute('data-profile-id'), committed.id);
  await inventory(); await read();
  scenarios.push('R05');
  // A replaced setup cannot select a profile from its late reconciliation GET.
  await click('Cerrar sesión'); await login();
  await selectSource(); await validate(); await notice('Conexión validada.');
  await page.evaluate(() => {
    const original = window.fetch;
    window.p01OriginalFetch = original;
    window.fetch = async (...args) => {
      const result = await original(...args);
      if (new URL(String(args[0])).pathname === '/api/v1/connections' && (!args[1]?.method || args[1].method === 'GET')) {
        window.p01ReconcileHeld = true;
        await new Promise(resolve => { window.p01ReconcileRelease = resolve; });
      }
      return result;
    };
  });
  await click('Crear sin activar');
  await page.waitForFunction(() => window.p01ReconcileHeld);
  await click('Home Assistant');
  await page.evaluate(() => {
    window.fetch = window.p01OriginalFetch;
    window.p01ReconcileRelease();
  });
  // A fresh setup request acts as a network/event-loop barrier after the released promise.
  await click('Buscar sistemas');
  await click('Seleccionar Salón'); await notice('Sistema seleccionado.');
  assert.equal(await page.getByRole('button', { name: 'Perfil seleccionado', exact: true }).count(), 0);
  assert.equal(await page.locator('#create').isEnabled(), false);
  await click('Cancelar configuración');
  await click('Seleccionar perfil'); await inventory(); await read();


  // R06: explicit presentation faults. Real provider/state negatives are covered by backend tests.
  for (const diagnostic of ['state_missing', 'stale', 'invalid', 'unavailable', 'source_unavailable']) {
    await page.route('**/api/v1/onboarding/state/**', async route => {
      const result = await route.fetch(); const body = await result.json();
      await route.fulfill({ response: result, json: { ...body, read_status: 'pending', states: [], diagnostics: [diagnostic] } });
    });
    await click('Ver lectura de temperature');
    await page.waitForFunction(() => document.querySelector('#reading').textContent.includes('Puedes volver a consultar.'));
    assert.equal(await page.locator('#browser-evidence').textContent(), '{}');
    assert.doesNotMatch(await page.locator('#reading').textContent(), /Lectura verificada/);
    await page.unroute('**/api/v1/onboarding/state/**');
  }
  await page.route('**/api/v1/onboarding/inventory', async route => {
    const result = await route.fetch(); const body = await result.json();
    await route.fulfill({ response: result, json: { ...body, devices: [] } });
  });
  await click('Actualizar inventario');
  await page.getByText('Sin dispositivos encontrados. Primera lectura pendiente.', { exact: true }).waitFor();
  assert.equal(await page.locator('#browser-evidence').textContent(), '{}');
  await page.unroute('**/api/v1/onboarding/inventory');
  scenarios.push('R06');

  // Manual rendering follows any selected provider's actual manifest.
  const others = await page.locator('#catalog button').allTextContents();
  await click(others.find(name => name !== 'Home Assistant'));
  if (await page.locator('#manual').isVisible()) {
    await click('Configurar manualmente');
    assert.ok(await page.locator('#setup-fields input,#setup-fields select').count());
    await click('Cancelar configuración');
  }

  // R09: untrusted labels remain text, secrets never enter storage/URL/output.
  await click('Cerrar sesión'); await assertClean();
  await page.route('**/api/v1/connection-providers', async route => {
    const result = await route.fetch(); const body = await result.json();
    body.providers[0].display_name = '<img src=x onerror="window.p01Injected=true">';
    await route.fulfill({ response: result, json: body });
  });
  await login();
  assert.equal(await page.locator('#catalog img').count(), 0);
  assert.equal(await page.evaluate(() => Boolean(window.p01Injected)), false);
  await page.unroute('**/api/v1/connection-providers');
  await click('Seleccionar perfil'); await inventory(); await read();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
  await click('Cerrar sesión'); await assertClean();

  // Delay completion after the response crossed the network boundary. Aborting fetch alone
  // cannot invalidate already-resolved promises; the session epoch must do so.
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = async (...args) => {
      const response = await original(...args);
      if (window.p01Hold && String(args[0]).includes('/onboarding/inventory')) {
        window.p01Held = true;
        await new Promise(resolve => { window.p01Release = resolve; });
        return new Response('{}', { status: 401, headers: { 'content-type': 'application/json' } });
      }
      return response;
    };
  });
  await login();
  await page.evaluate(() => { window.p01Hold = true; });
  await click('Actualizar inventario');
  await page.waitForFunction(() => window.p01Held);
  await click('Cerrar sesión'); await assertClean();
  await login(readToken);
  await page.getByText('Credencial comprobada por el servidor. Acceso de solo lectura.', { exact: true }).waitFor();
  await page.evaluate(() => { window.p01Hold = false; window.p01Release(); });
  await page.waitForFunction(() => document.querySelector('#inventory').disabled === false);
  assert.equal(await page.locator('#app').isVisible(), true);
  assert.equal(await page.locator('#catalog button').first().isEnabled(), false);
  assert.equal(await page.getByRole('button', { name: 'Actualizar credencial', exact: true }).count(), 0);
  await click('Cerrar sesión'); await assertClean();

  // A current 401 does end the session and removes populated private DOM.
  await login(); await click('Seleccionar perfil'); await inventory(); await read();
  await page.route('**/api/v1/onboarding/inventory', route => route.fulfill({ status: 401, json: {} }));
  await click('Actualizar inventario');
  await notice('La credencial no es válida o ha caducado. Accede de nuevo.');
  await assertClean();
  await page.unroute('**/api/v1/onboarding/inventory');
  scenarios.push('R08', 'R09');
  assert.deepEqual(errors, []);
  return {
    status: 'PASS', http_status: response.status(), configured_viewport: page.viewportSize(),
    overflow: false, logged_out: true, scenarios,
    evidence, negative_read_scope: 'UI presentation with explicit response fault injection',
    tooling: { node: process.version, playwright: require('playwright/package.json').version, browser: browser.version() }
  };
}

async function formRegression(page, mode) {
  const click = name => page.getByRole('button', { name, exact: true }).click();
  const notice = text => page.getByText(text, { exact: true }).waitFor();
  await page.getByLabel('Credencial de administración', { exact: true }).fill(token);
  await page.locator('#management-token').press('Enter'); await notice('Acceso confirmado.');
  if (mode === 'manual-form') {
    await click('Home Assistant'); await click('Configurar manualmente');
    assert.equal(await page.locator('#create').isEnabled(), false, 'empty manual form must disable create');
    await page.getByLabel('URL de Home Assistant', { exact: true }).fill('invalid-url');
    await page.getByLabel('Token de acceso', { exact: true }).fill('synthetic-ha-fixture');
    assert.equal(await page.locator('#create').isEnabled(), false, 'invalid URL must disable create');
    await page.getByLabel('URL de Home Assistant', { exact: true }).fill(haUrl);
    assert.equal(await page.locator('#create').isEnabled(), true);
    await page.getByLabel('Token de acceso', { exact: true }).fill('');
    assert.equal(await page.locator('#create').isEnabled(), false);
    await page.getByLabel('Token de acceso', { exact: true }).fill('invalid-ha-fixture');
    await click('Crear sin activar');
    await notice('No se confirmó la creación. Reintentar con una credencial nueva; se conservará la identidad.');
    assert.equal(await page.getByLabel('Token de acceso', { exact: true }).inputValue(), '');
    assert.equal(await page.locator('#create').isEnabled(), false, 'secret clearing after error must disable create');
    await page.getByLabel('Token de acceso', { exact: true }).fill('synthetic-ha-fixture');
    assert.equal(await page.locator('#create').isEnabled(), true);
    await click('Crear sin activar'); await notice('Conexión creada sin activar.');
  } else if (mode === 'validation-edit') {
    await click('Home Assistant'); await click('Buscar sistemas'); await click('Seleccionar Salón');
    await notice('Sistema seleccionado.');
    for (const [phase, field] of [['setup', 'url'], ['validate', 'url'], ['validate', 'token']]) {
      await page.evaluate(phase => {
        const original = window.fetch;
        window.p01BeforeDelay = original; window.p01DelayReady = false;
        window.fetch = async (...args) => {
          const result = await original(...args);
          if (args[1]?.body && JSON.parse(args[1].body).action === phase) {
            window.p01DelayReady = true;
            await new Promise(resolve => { window.p01DelayRelease = resolve; });
          }
          return result;
        };
      }, phase);
      await page.getByLabel('Token de acceso', { exact: true }).fill('synthetic-ha-fixture');
      await click('Validar conexión'); await page.waitForFunction(() => window.p01DelayReady);
      await page.locator('#setup-' + field).fill(field === 'url' ? 'http://127.0.0.1:1' : 'invalid-ha-fixture');
      assert.equal(await page.locator('#create').isEnabled(), false);
      await page.evaluate(() => { window.fetch = window.p01BeforeDelay; window.p01DelayRelease(); });
      // Re-enabling validation marks completion of the old handler, including its finally.
      await page.waitForFunction(() => !document.querySelector('#validate').disabled);
      assert.equal(await page.locator('#create').isEnabled(), false, `${phase} completion must not validate edited ${field}`);
      assert.notEqual(await page.locator('#notice').textContent(), 'Conexión validada.');
      await page.getByLabel('URL de Home Assistant', { exact: true }).fill(haUrl);
      await page.getByLabel('Token de acceso', { exact: true }).fill('synthetic-ha-fixture');
      await click('Validar conexión'); await notice('Conexión validada.');
    }
    await click('Crear sin activar'); await notice('Conexión creada sin activar.');
  } else if (mode === 'mapping') {
    const mapping = process.env.P01_BROWSER_MAPPING;
    assert.ok(mapping);
    const bodies = [];
    page.on('request', request => {
      const pathname = new URL(request.url()).pathname;
      if (request.method() === 'POST' && (pathname === '/api/v1/connections' || pathname === '/api/v1/connections/setup')) {
        const body = request.postDataJSON();
        if (pathname === '/api/v1/connections' || body.action === 'setup') bodies.push(body);
      }
    });
    await click('KNX/IP'); await click('Configurar manualmente');
    await page.getByLabel('Gateway host', { exact: true }).fill('127.0.0.1');
    await page.getByLabel('Gateway port', { exact: true }).fill('3671');
    await page.getByLabel('Mapping file', { exact: true }).fill(mapping);
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/v1/connections' && r.request().method() === 'POST');
    await click('Crear sin activar');
    const created = await response;
    assert.equal(bodies[0].mapping_ref, mapping, 'manual mapping_ref belongs at API top level');
    assert.equal(Object.hasOwn(bodies[0].options, 'mapping_ref'), false);
    assert.equal(created.status(), 201);
    await notice('Conexión creada sin activar.');
    const firstId = await page.locator('[data-profile-id]').getAttribute('data-profile-id');
    // Same manual identity remains stable when the mapping is represented correctly.
    await click('KNX/IP'); await click('Configurar manualmente');
    await page.getByLabel('Gateway host', { exact: true }).fill('127.0.0.1');
    await page.getByLabel('Gateway port', { exact: true }).fill('3671');
    await page.getByLabel('Mapping file', { exact: true }).fill(mapping);
    await click('Crear sin activar');
    await notice('Perfil existente recuperado. Continúa con el mismo perfil sin crear otra conexión.');
    assert.equal(await page.locator('[data-profile-id]').getAttribute('data-profile-id'), firstId);
    await click('KNX/IP'); await click('Buscar sistemas'); await click('Seleccionar KNX de ensayo');
    await notice('Sistema seleccionado.');
    await page.getByLabel('Mapping file', { exact: true }).fill(mapping);
    const configured = page.waitForResponse(r => r.request().method() === 'POST' && r.request().postDataJSON()?.action === 'setup');
    await click('Validar conexión'); const setup = await configured;
    const body = bodies.at(-1);
    assert.equal(body.mapping_ref, mapping, 'discovery mapping_ref belongs at API top level');
    assert.equal(Object.hasOwn(body.options, 'mapping_ref'), false);
    assert.equal(setup.status(), 200); await notice('Conexión validada.');
    await click('Crear sin activar'); await notice('Conexión creada sin activar.');
    await click('Activar conexión'); await notice('Conexión activada.');
    assert.equal(await page.locator('[data-profile-id]').count(), 2);
  }
  await click('Cerrar sesión');
  return { status: 'PASS', regression: mode };
}

async function manualProduct(page, response) {
  const click = name => page.getByRole('button', { name, exact: true }).click();
  await page.getByLabel('Credencial de administración', { exact: true }).fill(token);
  await page.locator('#management-token').press('Enter');
  await page.getByText('Acceso confirmado.', { exact: true }).waitFor();
  await click('Home Assistant'); await click('Configurar manualmente');
  await page.getByLabel('URL de Home Assistant', { exact: true }).fill(haUrl);
  await page.getByLabel('Token de acceso', { exact: true }).fill('synthetic-ha-fixture');
  let committed;
  await page.route('**/api/v1/connections', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const result = await route.fetch(); assert.equal(result.status(), 201);
    committed = (await result.json()).connection;
    assert.equal(committed.enabled, false);
    await route.abort('connectionreset');
  });
  await click('Crear sin activar');
  await page.getByText('Perfil existente recuperado. Continúa con el mismo perfil sin crear otra conexión.', { exact: true }).waitFor();
  await page.unroute('**/api/v1/connections');
  assert.match(committed.unique_id, /^home_assistant:manual:[a-f0-9]{16}$/);
  assert.equal(await page.locator('[data-profile-id]').getAttribute('data-profile-id'), committed.id);
  // Re-entering exactly the same endpoint produces the same identity, never a random retry ID.
  await click('Home Assistant'); await click('Configurar manualmente');
  await page.getByLabel('URL de Home Assistant', { exact: true }).fill(haUrl);
  await page.getByLabel('Token de acceso', { exact: true }).fill('synthetic-ha-fixture');
  await click('Crear sin activar');
  await page.waitForFunction(() => document.querySelector('#setup').hidden);
  assert.equal(await page.locator('[data-profile-id]').count(), 1);
  assert.equal(await page.locator('[data-profile-id]').getAttribute('data-profile-id'), committed.id);
  assert.match(await page.locator('[data-profile-id]').textContent(), /Sin activar/);
  await click('Cerrar sesión');
  assert.equal(await page.locator('#setup-fields').textContent(), '');
  return { status: 'PASS', profile_id: committed.id, identity: committed.unique_id, http_status: response.status(), scope: 'real manual create, lost response and exact identity retry' };
}

(async () => {
  assert.ok(baseUrl && evidencePath && (token || authorization));
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      viewport: { width: 360, height: 800 }, isMobile: true,
      ...(token ? {} : { extraHTTPHeaders: { authorization } })
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    const response = await page.goto(baseUrl, { waitUntil: 'networkidle' });
    const result = token ? (['manual-form', 'validation-edit', 'mapping'].includes(process.env.P01_BROWSER_MODE) ? await formRegression(page, process.env.P01_BROWSER_MODE) : process.env.P01_BROWSER_MODE === 'manual' ? await manualProduct(page, response) : await product(browser, context, page, response)) : {
      status: 'TOOLINGPASS', scope: 'T003 Chromium and loopback HTTP only; no product acceptance',
      configured_viewport: page.viewportSize(), http_status: response.status(),
      browser_path_configured: Boolean(process.env.PLAYWRIGHT_BROWSERS_PATH),
      tooling: { node: process.version, playwright: require('playwright/package.json').version, browser: browser.version() }
    };
    fs.writeFileSync(evidencePath, `${JSON.stringify(result, null, 2)}\n`);
    console.log(JSON.stringify(result));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
