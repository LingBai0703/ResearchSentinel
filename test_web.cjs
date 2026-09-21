// Run with Node.js and Playwright installed. Uses only isolated, temporary jobs.
const {chromium} = require("playwright");
const {spawn} = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const assert = require("node:assert/strict");

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const result = await fn().catch(() => false);
    if (result) return result;
    await sleep(200);
  }
  throw new Error("Timed out waiting for test condition");
}
async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "research-sentinel-test-"));
  const listener = net.createServer();
  await new Promise(resolve => listener.listen(0, "127.0.0.1", resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const url = `http://127.0.0.1:${port}`;
  const python = process.env.PYTHON_BIN || "python";
  const server = spawn(python, [path.join(__dirname, "research_server.py"), "--project", root, "--data-dir", path.join(root, "data"), "--port", String(port)], {cwd: root, windowsHide: true});
  const children = [server];
  let errors = "";
  server.stderr.on("data", chunk => { errors += chunk; });
  let browser;
  try {
    const status = async () => (await fetch(`${url}/api/status`)).json();
    await until(async () => (await status()).ready);
    const worker = spawn(python, [path.join(__dirname, "tests/research_fixture.py"), root], {
      cwd: root,
      windowsHide: true,
      env: {...process.env, RESEARCH_SENTINEL_DATA_DIR: path.join(root, "data")},
    });
    children.push(worker);
    await until(async () => (await status()).tasks.length === 1);
    browser = await chromium.launch({channel: "msedge", headless: true});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const pageErrors = [];
    page.on("pageerror", error => pageErrors.push(error.message));
    await page.goto(`${url}/#processes`);
    await page.locator(".process-card").waitFor();
    await page.locator(".process-toggle").click();
    await page.locator(".process-details:visible").waitFor();
    await page.locator('[name="output_dir"]').fill(root);
    await page.getByRole("button", {name: "保存路径", exact: true}).click();
    await page.locator(".task-restart").uncheck();
    await until(async () => !(await status()).tasks[0].auto_restart);
    await page.locator(".task-restart").check();
    await until(async () => (await status()).tasks[0].auto_restart);
    await until(async () => (await status()).tasks[0].status === "COMPLETED");
    await until(async () => (await status()).tasks[0].processes.length === 0);
    await page.locator(".result-preview img").waitFor();
    await until(async () => page.locator(".result-preview img").evaluate(img => img.complete && img.naturalWidth > 0));
    await page.screenshot({path: path.join(root, "desktop.png"), fullPage: true});
    await page.getByRole("button", {name: "设置", exact: true}).click();
    await page.locator('[name="refresh_seconds"]').fill("3");
    await page.getByRole("button", {name: "保存设置", exact: true}).click();
    await until(async () => (await status()).settings.refresh_seconds === 3);
    // Exercise both network binding directions without touching the real service.
    const post = async body => {
      const r = await fetch(`${url}/api/settings`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
      assert.equal(r.status, 200);
    };
    await post({lan_enabled: true});
    await sleep(800);
    await until(async () => (await status()).settings.lan_enabled);
    await post({lan_enabled: false});
    await sleep(800);
    await until(async () => !(await status()).settings.lan_enabled);
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(root, "settings-mobile.png"), fullPage: true});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.getByRole("button", {name: /^进程/}).click();
    await page.screenshot({path: path.join(root, "task-mobile.png"), fullPage: true});
    await page.locator(".task-delete").click();
    assert.equal(await page.locator("#deleteStop").isChecked(), false);
    await page.getByRole("button", {name: "删除任务", exact: true}).click();
    await until(async () => (await status()).tasks.length === 0);
    await page.reload();
    await page.getByText("暂无科研任务", {exact: true}).waitFor();
    assert.deepEqual(pageErrors, []);
    assert.equal(errors, "");
    console.log(JSON.stringify({ok: true, screenshots: root, checks: [
      "Python discovery", "per-task toggle", "completion retention", "results",
      "settings persistence", "LAN rebind both directions", "mobile overflow",
      "delete defaults to not stopping", "no console errors"
    ]}));
  } finally {
    if (browser) await browser.close();
    for (const child of children) {
      if (child.exitCode === null) child.kill();
      if (child.exitCode === null) await new Promise(resolve => child.once("exit", resolve));
    }
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
