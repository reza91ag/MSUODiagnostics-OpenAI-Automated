import { randomUUID } from 'node:crypto';
import { open } from 'node:fs/promises';
import { chromium } from 'playwright';

const apps = {
  entryflow: { host: 'entryflow.msuo-runtime.io', pdf: 'Generate PDF' },
  optispec: { host: 'optispec.msuo-runtime.io', pdf: 'Generate Report', pdfToolbar: 'PDF' },
};
const appName = process.argv[2];
const app = apps[appName];
const startupOnly = process.argv.includes('--startup-only');
const { MSUO_QA_EMAIL: email, MSUO_QA_PASSWORD: password } = process.env;
if (!Object.hasOwn(apps, appName)) throw new Error('Select entryflow or optispec.');
if (!startupOnly && (!email || !password)) throw new Error('Both QA secrets are required.');

const recoveryId = process.env.QA_RECOVERY_ENTRY_ID;
if (recoveryId && !/^qa-[0-9a-f]{8}-[0-9a-f]{3}$/.test(recoveryId)) {
  throw new Error('Recovery requires a synthetic record ID.');
}
const runId = recoveryId || 'qa-' + randomUUID().slice(0, 12);
const timeout = 25000;
const browser = await chromium.launch({ channel: 'chrome' });
const page = await browser.newPage({ viewport: { width: 1365, height: 936 }, acceptDownloads: true });
page.setDefaultTimeout(timeout);

let stage = 'startup';
let possibleRecord = false;
const exact = { exact: true };
const ui = text => page.getByLabel(text, exact).or(page.getByText(text, exact)).filter({ visible: true });
const button = name => page.getByRole('button', { name, exact: true }).filter({ visible: true });
const deviceRows = () => ui(/^Device-ID:\s*\S+/i);
const knownDevices = new Set([
  'qa-10c6b569-bd3', 'qa-5ff37d37-ea8', 'qa-836aa8f3-ade', 'qa-a6e3cf5b-1a4', 'qa-3166639d-810', runId,
]);
let selectedDeviceId;
let removalStarted = false;
const row = () => appName === 'optispec'
  ? ui(new RegExp('^Device-ID:\\s*' + (selectedDeviceId ?? runId) + '(?=\\s|$)', 'i'))
  : ui(new RegExp('^(?:Holder:\\s*)?' + runId + '(?=\\s|$)', 'i'));
const signOutControl = () => {
  const name = /^sign\s*out$/i;
  return page.getByRole('button', { name })
    .or(page.getByRole('link', { name })).or(ui(name)).filter({ visible: true });
};

async function enableSemantics() {
  const enable = page.locator('flt-semantics-placeholder[aria-label="Enable accessibility"]');
  const attached = await enable.waitFor({ state: 'attached', timeout: 4000 }).then(() => true, () => false);
  if (attached) await enable.dispatchEvent('click');
}

async function splash() {
  await page.goto('https://' + app.host + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForURL(url => ['/splashScreen', '/authorization'].includes(url.pathname), { timeout });
  if (new URL(page.url()).pathname === '/splashScreen') {
    await page.locator('flt-glass-pane').waitFor({ state: 'attached' });
    // Allow the Flutter canvas to paint before clicking the branded splash.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.mouse.click(682, 455);
  }
  await page.waitForURL(url => url.hostname === app.host && url.pathname === '/authorization', { timeout });
  await enableSemantics();
  await authField('Email').waitFor();
  await authField('Password').waitFor();
  await button('Sign In').waitFor();
}

function authField(name) {
  return page.locator('input[data-semantics-role="text-field"]').and(page.getByLabel(name, exact))
    .or(page.getByPlaceholder(name, exact)).filter({ visible: true });
}

async function clickControl(control) {
  await control.waitFor();
  await control.click({ force: true });
}

async function fillField(name, value) {
  const field = authField(name);
  await clickControl(field);
  const focused = page.locator('input:focus, textarea:focus');
  await focused.waitFor();
  await page.keyboard.press('Control+a');
  await page.keyboard.insertText(value);
  if (!await focused.evaluate((element, expected) => element.value === expected, value)) {
    throw new Error('The control did not accept the value.');
  }
}

async function signIn() {
  const signInStage = stage;
  stage = signInStage + ' / email field';
  await fillField('Email', email);
  stage = signInStage + ' / password field';
  await fillField('Password', password);
  stage = signInStage + ' / submit';
  const ingress = 'https://' + appName + '-api.msuo-runtime.io/ingress';
  let sent = false;
  let status;
  const onRequest = request => {
    if (request.url() === ingress && request.method() === 'POST') sent = true;
  };
  const onResponse = response => {
    if (response.url() === ingress && response.request().method() === 'POST') {
      status ??= response.status();
    }
  };
  page.on('request', onRequest);
  page.on('response', onResponse);
  try {
    await clickControl(button('Sign In'));
    stage = signInStage + ' / wait for dashboard';
    await page.waitForURL(url => url.hostname === app.host && /^\/dashboard\/?$/.test(url.pathname));
    await enableSemantics();
    const ready = appName === 'optispec' ? signOutControl().or(button('PDF')) : signOutControl();
    await ready.first().waitFor();
    stage = signInStage;
  } catch (error) {
    const atSignIn = new URL(page.url()).pathname === '/authorization';
    console.log('Sign-in diagnostic: POST sent=' + sent + ', HTTP status=' + (status ?? 'none') +
      ', still on sign-in screen=' + atSignIn);
    if (atSignIn) {
      const messages = [
        'Authentication failed.',
        'Invalid or missing E-Mail.',
        'E-Mail does not exist.',
        'Unable to connect to the server. Please try again.',
      ];
      for (const message of messages) {
        if (await ui(message).first().isVisible()) console.log('Sign-in message: ' + message);
      }
    }
    throw error;
  } finally {
    page.off('request', onRequest);
    page.off('response', onResponse);
  }
}

async function signOut() {
  await clickControl(signOutControl().first());
  await page.waitForURL(url => url.hostname === app.host && url.pathname === '/authorization', { timeout });
  await enableSemantics();
  await authField('Email').waitFor();
  await authField('Password').waitFor();
  await button('Sign In').waitFor();
}

async function footerControl(kind) {
  if (appName === 'optispec') {
    await clickControl(button(kind === 'add' ? 'Add' : 'Remove'));
    return;
  }
  const names = kind === 'add' ? /^(add (entry|device)|plus|\+)$/i : /^(remove (entry|device)|minus|[-−])$/i;
  const named = page.getByRole('button', { name: names }).filter({ visible: true });
  if (await named.count() === 1) {
    await clickControl(named);
    return;
  }

  const controls = await page.locator('flt-semantics[role="button"]').evaluateAll(nodes =>
    nodes.map(node => {
      const { x, y, width, height } = node.getBoundingClientRect();
      return { label: node.getAttribute('aria-label'), x, y, width, height };
    }).filter(({ label, x, y, width, height }) => !label && x > innerWidth - 240 &&
      y > innerHeight - 110 && width >= 32 && width <= 60 && height >= 32 && height <= 60)
      .sort((left, right) => left.x - right.x)
  );
  if (controls.length !== 2 || Math.abs(controls[0].y - controls[1].y) > 5) {
    // EntryFlow's icon-only footer can omit the disabled minus button from semantics.
    // The fixed desktop viewport matches the app's desktop footer layout.
    const viewport = page.viewportSize();
    if (viewport?.width !== 1365 || viewport.height !== 936) {
      throw new Error('Could not identify the desktop footer controls.');
    }
    await page.mouse.click(viewport.width - (kind === 'add' ? 108 : 36), viewport.height - 32);
    return;
  }
  const { x, y, width, height } = controls[kind === 'add' ? 0 : 1];
  await page.mouse.click(x + width / 2, y + height / 2);
}

async function addRecord() {
  await footerControl('add');
  stage = 'fill synthetic record form';
  if (appName === 'entryflow') {
    await fillField('Name', runId);
    await fillField('Amount', '1');
    await fillField('Account', 'QA Test');
    stage = 'select EntryFlow currency';
    const currency = ui('Currency').or(button('Currency')).first();
    if (await currency.isVisible()) {
      await clickControl(currency);
      await clickControl(page.getByRole('option', { name: 'EUR', exact: true }).or(ui('EUR')).first());
    } else {
      // A remembered EUR choice can be shown as the selected value.
      await ui('EUR').or(button('EUR')).first().waitFor();
    }
    stage = 'submit EntryFlow synthetic record';
    possibleRecord = true;
    const submit = button('Confirm');
    await clickControl(submit);
    console.log('PASS: clicked EntryFlow Confirm');
    stage = 'wait for EntryFlow entry form to close';
    try {
      await authField('Name').waitFor({ state: 'hidden' });
    } catch (error) {
      for (const message of [
        'Amount must be a valid number.',
        'Date must be a real calendar day in DD/MM/YYYY.',
        'An exchange rate is required before this entry can be sent.',
        'The entry could not be saved.',
        'Server communication failure. Please try again.',
      ]) {
        if (await ui(message).first().isVisible()) console.log('EntryFlow form message: ' + message);
      }
      throw error;
    }
  } else {
    await fillField('Device-ID', runId);
    await fillField('URL', 'http://192.0.2.1:9/qa');
    await fillField('Device-Group', runId + '-group');
    stage = 'submit OptiSpec synthetic record';
    possibleRecord = true;
    const submit = button('Add Device');
    await clickControl(submit);
    await submit.waitFor({ state: 'hidden' });
  }
  stage = 'verify synthetic record on dashboard';
  await enableSemantics();
  if (appName === 'optispec') {
    const top = deviceRows().first();
    await top.waitFor();
    const label = await top.evaluate(element => element.getAttribute('aria-label') || element.textContent);
    const id = /^Device-ID:\s*(qa-[0-9a-f]{8}-[0-9a-f]{3})(?=\s|$)/i.exec(label?.trim() ?? '')?.[1];
    if (!knownDevices.has(id)) throw new Error('The top device is outside the known QA set.');
    selectedDeviceId = id;
    console.log('OptiSpec top QA device: ' + id + ', newly submitted=' + (id === runId));
  }
  await row().first().waitFor();
}

async function selectCalendarDay(date, column, headings) {
  const { from, till } = headings;
  const sideBySide = till.x - from.x >= 100;
  const label = new Intl.DateTimeFormat('en-US', {
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
  }).format(date);
  const dateCells = ui(label).or(button(label));
  await dateCells.first().waitFor();
  const centers = [];
  const above = Math.min(from.y, till.y);
  for (const cell of await dateCells.all()) {
    const rect = await cell.boundingBox();
    if (!rect) continue;
    const x = rect.x + rect.width / 2;
    const y = rect.y + rect.height / 2;
    if (y <= above || x < 0 || x >= 1365 || y >= 936) continue;
    if (!centers.some(other => Math.abs(x - other.x) < 8 && Math.abs(y - other.y) < 8)) {
      centers.push({ x, y });
    }
  }
  centers.sort((a, b) => sideBySide ? a.x - b.x : a.y - b.y);
  if (centers.length !== 2) {
    console.log('Calendar date matches within viewport: ' + centers.length);
    throw new Error('Expected one matching date in each calendar.');
  }
  const { x, y } = centers[column === 'From' ? 0 : 1];
  await page.mouse.click(x, y);
}

async function generatePdf() {
  stage = 'open PDF controls';
  await clickControl(button(app.pdfToolbar ?? app.pdf));
  await ui('From').waitFor();
  await ui('Till').waitFor();
  console.log('PASS: opened PDF dialog');
  const from = await ui('From').boundingBox();
  const till = await ui('Till').boundingBox();
  stage = 'locate From and Till calendars';
  if (!from || !till) throw new Error('Could not locate the two calendars.');
  console.log('Calendar layout: ' + (till.x - from.x >= 100 ? 'side by side' : 'stacked'));

  const today = await page.evaluate(() => {
    const now = new Date();
    return { year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate() };
  });
  stage = 'select From date';
  await selectCalendarDay(new Date(today.year, today.month - 1, 1, 12), 'From', { from, till });
  stage = 'select Till date';
  const lastDay = Math.max(1, today.day - 1);
  await selectCalendarDay(new Date(today.year, today.month - 1, lastDay, 12), 'Till', { from, till });
  console.log('PASS: clicked the From and Till calendar dates');
  stage = 'submit PDF request';
  const candidates = button(app.pdf);
  // EntryFlow's toolbar and form share the same label. The form button
  // is the one inside Flutter's active dialog route.
  const dialogButton = page.getByRole('dialog').getByRole('button', { name: app.pdf, exact: true });
  const submit = await dialogButton.count() === 1 ? dialogButton : candidates;
  if (await submit.count() !== 1) throw new Error('PDF action is ambiguous.');
  const [file] = await Promise.all([
    page.waitForEvent('download', { timeout }),
    clickControl(submit),
  ]);
  stage = 'verify downloaded PDF';
  const month = String(today.month).padStart(2, '0');
  const tillDay = String(lastDay).padStart(2, '0');
  const fileDateRange = `${today.year}-${month}-01_${today.year}-${month}-${tillDay}.pdf`;
  if (!file.suggestedFilename().endsWith(fileDateRange)) {
    throw new Error('The downloaded report does not match the selected date range.');
  }
  const path = await file.path();
  const handle = await open(path, 'r');
  try {
    const bytes = Buffer.alloc(5);
    const { bytesRead } = await handle.read(bytes, 0, 5, 0);
    if (bytesRead !== 5 || bytes.toString() !== '%PDF-') throw new Error('Invalid PDF download.');
  } finally {
    await handle.close();
  }
}

async function removeRecord() {
  removalStarted = true;
  if (!await row().first().isVisible()) return false;
  if (appName === 'entryflow' && await row().count() !== 1) return false;
  let message;
  if (appName === 'entryflow') {
    stage = 'select EntryFlow synthetic entry';
    const selected = page.locator('flt-semantics[aria-checked="true"], flt-semantics[aria-selected="true"]').filter({ visible: true });
    // Begin deletion only from a freshly signed-in dashboard, with no existing selection.
    if (await selected.count()) return false;
    await clickControl(row());
    stage = 'open EntryFlow remove dialog';
    await footerControl('remove');
    stage = 'verify the EntryFlow confirmation names one entry';
    await ui('Remove selected entries?').waitFor();
    message = ui('Do you really want to remove 1 selected entry?');
    await message.waitFor();
  } else {
    stage = 'select OptiSpec device row';
    await clickControl(row().first());
    stage = 'open OptiSpec remove dialog';
    const remove = button('Remove');
    await remove.and(page.getByRole('button', { disabled: false })).waitFor();
    await footerControl('remove');
    stage = 'verify OptiSpec remove dialog';
    const deviceOnly = ui('Do you really want to remove this device?');
    const deviceAndInitialEntry = ui('Do you really want to remove this device and all of its entries?');
    // Add Device creates an empty measurement entry for each synthetic device.
    // The target is already constrained to the exact test device ID.
    await deviceOnly.or(deviceAndInitialEntry).first().waitFor();
    message = await deviceOnly.first().isVisible() ? deviceOnly : deviceAndInitialEntry;
  }
  stage = 'confirm removal of selected synthetic record';
  const dialogRemove = page.getByRole('dialog').getByRole('button', { name: 'Remove', exact: true });
  const remove = button('Remove');
  if (await dialogRemove.count() === 1) {
    await clickControl(dialogRemove);
  } else if (await remove.count() === 1) {
    await clickControl(remove);
  } else {
    // The desktop OptiSpec toolbar also has a Remove button.
    const anchor = await message.boundingBox();
    if (!anchor) return false;
    const candidates = [];
    for (const control of await remove.all()) {
      const rect = await control.boundingBox();
      if (rect && Math.abs(rect.y - anchor.y) < 140 && rect.y > anchor.y) candidates.push(control);
    }
    if (candidates.length !== 1) return false;
    await clickControl(candidates[0]);
  }
  stage = 'verify selected synthetic record is gone';
  await row().first().waitFor({ state: 'hidden' });
  return true;
}

try {
  await splash();
  console.log('PASS: splash and sign-in form');
  if (!startupOnly) {
    stage = 'sign-in';
    await signIn();
    if (recoveryId) {
      stage = 'recover previous synthetic entry';
      possibleRecord = true;
      const empty = ui(appName === 'entryflow' ? 'No accounts or entries yet' : /^No devices/i);
      const found = await row().or(empty).first().waitFor().then(() => true, () => false);
      if (!found) {
        const markerVisible = await ui(new RegExp(runId)).first().isVisible();
        console.log('Cleanup diagnostic: synthetic identifier visible in dashboard=' + markerVisible);
        throw new Error('The exact synthetic row could not be located.');
      }
      if (await row().count() === 1 || (appName === 'optispec' && await row().first().isVisible())) {
        stage = 'remove located synthetic entry';
        if (!await removeRecord()) throw new Error('Could not safely identify the entry for removal.');
        console.log('PASS: previous synthetic entry removed');
      } else if (await empty.isVisible() && await row().count() === 0) {
        console.log('PASS: dashboard is empty');
      } else {
        throw new Error('The entry could not be located unambiguously.');
      }
      possibleRecord = false;
    } else {
      stage = 'sign-out';
      await signOut();
      stage = 'sign-in again';
      await signIn();
      console.log('PASS: sign-in, sign-out, and sign-in again');
      stage = 'PDF download';
      await generatePdf();
      console.log('PASS: downloaded file has a PDF signature');
      if (process.env.QA_PDF_ONLY !== 'true') {
        stage = 'create synthetic record';
        await addRecord();
        console.log('PASS: synthetic test row visible');
        stage = 'remove synthetic record';
        if (!await removeRecord()) throw new Error('Could not safely identify the synthetic record.');
        console.log('PASS: selected synthetic record removed and no longer visible');
        if (selectedDeviceId && selectedDeviceId !== runId) {
          stage = 'verify newly submitted OptiSpec device';
          throw new Error('An older QA device was removed. The new device remains unverified.');
        }
        possibleRecord = false;
      }
    }
  }
} catch {
  // Playwright errors can include form data, so report the stage without exception details.
  console.error('::error title=Browser smoke test failed::' + stage);
  if (possibleRecord && !recoveryId && !removalStarted) {
    try {
      // Reset any open dialog and selection while staying inside the same test account.
      await page.reload({ waitUntil: 'domcontentloaded' });
      await enableSemantics();
      await signOutControl().first().waitFor();
      if (await removeRecord()) possibleRecord = false;
    } catch {
      // Leave any ambiguous state for inspection without attempting another deletion.
    }
  }
  if (possibleRecord) console.error('::error title=Cleanup unverified::Synthetic record ' + runId);
  process.exitCode = 1;
} finally {
  await browser.close();
}
