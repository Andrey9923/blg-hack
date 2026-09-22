// npm install --no-save playwright; PLAYWRIGHT_CHANNEL=msedge on Windows if needed.
const { chromium } = require('playwright');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const readline = require('node:readline');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'operator-ui-'));
  const server = spawn(process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['-u','-c', `
import sys
from web.app import OperatorServer, OperatorService
from web.persistence import Database, SQLiteRunStore
server = OperatorServer(('127.0.0.1', 0), OperatorService(SQLiteRunStore(Database(sys.argv[1]))))
print(server.server_address[1], flush=True)
server.serve_forever()
`, path.join(temp, 'operator.sqlite3')], {cwd:path.resolve(__dirname, '..'), stdio:['ignore','pipe','inherit']});
  let browser;
  try {
    const port = await new Promise((resolve,reject) => {
      const timer = setTimeout(() => reject(new Error('Server startup timed out')), 15000);
      readline.createInterface({input:server.stdout}).once('line', line => { clearTimeout(timer); resolve(line); });
      server.once('error', reject);
    });
    browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
    const page = await browser.newPage({viewport:{width:1440,height:1000}, reducedMotion:'reduce'});
    const errors = [], failed = [], external = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('response', r => { if (r.status() >= 400) failed.push(`${r.status()} ${r.url()}`); });
    page.on('request', r => { if (!r.url().startsWith(`http://127.0.0.1:${port}`)) external.push(r.url()); });
    await page.goto(`http://127.0.0.1:${port}`);
    await page.waitForFunction(() => document.querySelector('#scenario').options.length === 4);
    await page.locator('#createBtn').click();
    await page.waitForFunction(() => document.querySelector('#stepNow').textContent === '0');
    await page.locator('#scheduleSave').click();
    await page.waitForFunction(() => document.querySelectorAll('#scheduleList button').length === 1);
    await page.locator('#schedulePreview').click();
    await page.waitForFunction(() => document.querySelector('#forecastInfo').textContent.startsWith('Прогноз 0–48'));
    assert.equal(await page.locator('#stepNow').textContent(), '0');
    assert(await page.locator('.timeline-cell.forecast').count() > 0);
    const source = page.locator('[data-edit-step="0"][data-edit-sat="S01"]');
    const target = page.locator('[data-edit-step="1"][data-edit-sat="S01"]');
    await source.dragTo(target);
    await page.waitForFunction(() => document.querySelector('#scheduleList').textContent.includes('S01 · 1 · idle'));
    await page.locator('#advanceSix').click();
    await page.waitForFunction(() => document.querySelector('#stepNow').textContent === '6');
    await page.locator('#advanceSix').click();
    await page.waitForFunction(() => document.querySelector('#stepNow').textContent === '12');
    await page.locator('[data-tab="trace"]').click();
    await page.locator('#traceNext').click();
    assert.match(await page.locator('#tracePage').textContent(), /2 \/ 2/);
    await page.locator('#traceFilter').fill('S01');
    assert.match(await page.locator('#tracePage').textContent(), /12 записей/);
    await page.locator('#trace .trace-link').first().click();
    await page.waitForFunction(() => !!document.querySelector('#explanation .pill'));
    await page.locator('summary').filter({hasText:'Сравнить несколько'}).click();
    await page.locator('#chartChoices input[value="S01"]').check();
    await page.locator('#chartChoices input[value="S02"]').check();
    assert.equal(await page.locator('#telemetryChart svg').count(), 2);
    assert.equal(await page.locator('#telemetryChart svg').first().locator('path').count(), 2);
    await page.locator('#chartFrom').fill('5');
    await page.locator('#chartTo').fill('8');
    assert.match(await page.locator('#chartStats').textContent(), /Шаги 5–8/);
    for (const width of [1440, 820, 390]) {
      await page.setViewportSize({width,height:1000});
      for (const theme of ['dark','light']) {
        await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Horizontal overflow: ${width} ${theme}`);
      }
    }
    await page.evaluate(() => document.fonts.ready);
    assert(await page.evaluate(() => [...document.fonts].some(font => font.family === 'Montserrat' && font.status === 'loaded')));
    assert.deepEqual(errors, []);
    assert.deepEqual(failed, []);
    assert.deepEqual(external, []);
    console.log('Browser PASS: schedule/drag/forecast, execution, full trace pagination/filter/explain, multi-satellite charts/range, local fonts, both themes/mobile, no external requests or JS errors.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) { server.kill(); await once(server, 'exit'); }
    fs.rmSync(temp, {recursive:true, force:true});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
