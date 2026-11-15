/* The real extension in an installed Chrome or Edge, driven over a DevTools pipe.
 *
 * node browser_extension_real_check.js BROWSER_EXE EXTENSION_DIR PROFILE_DIR BRIDGE_URL [SHOTS_DIR|-]
 *
 * The browser runs headless with a new, throwaway profile. Every x.com request
 * is answered here with a synthetic page, host names other than 127.0.0.1 do
 * not resolve, and the X cookies are synthetic, so nothing leaves this PC and
 * no real account is involved. Clicks are real input events (isTrusted), which
 * the consent page requires. Prints one JSON line.
 */
"use strict";
const {spawn} = require("node:child_process");
const {StringDecoder} = require("node:string_decoder");
const fs = require("node:fs");
const path = require("node:path");

const [browserPath, extensionPath, profileDir, bridgeUrl, shotsDir = "-"] = process.argv.slice(2);
const X_PAGE = '<!doctype html><html><body><a data-testid="AppTabBar_Profile_Link" href="https://x.com/Offline_User">Profile</a>' +
  '<button data-testid="SideNav_AccountSwitcher_Button"><div>오프라인 계정</div><div>@Offline_User</div></button></body></html>';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

class Pipe {
  constructor(write, read) {
    this.write = write; this.id = 0; this.pending = new Map(); this.listeners = [];
    const decoder = new StringDecoder("utf8"); let buffer = "";
    read.on("data", chunk => {
      buffer += decoder.write(chunk);
      let index;
      while ((index = buffer.indexOf("\0")) >= 0) {
        const message = JSON.parse(buffer.slice(0, index)); buffer = buffer.slice(index + 1);
        const waiting = message.id !== undefined && this.pending.get(message.id);
        if (waiting) {
          this.pending.delete(message.id);
          if (message.error) waiting.reject(new Error(`${waiting.method}: ${message.error.message}`));
          else waiting.resolve(message.result);
        } else for (const listener of this.listeners) listener(message);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id; const message = {id, method, params};
    if (sessionId) message.sessionId = sessionId;
    this.write.write(JSON.stringify(message) + "\0");
    return new Promise((resolve, reject) => this.pending.set(id, {resolve, reject, method}));
  }
}

(async () => {
  const browser = spawn(browserPath, [
    "--headless=new", `--user-data-dir=${profileDir}`, "--remote-debugging-pipe", "--enable-unsafe-extension-debugging",
    "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--disable-default-apps", "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "about:blank"
  ], {stdio: ["ignore", "ignore", "ignore", "pipe", "pipe"]});
  const pipe = new Pipe(browser.stdio[3], browser.stdio[4]);
  const result = {ok: false};
  const pages = new Map();
  let xRequests = 0;
  pipe.listeners.push(async message => {
    try {
      if (message.method === "Target.attachedToTarget") {
        const {sessionId, targetInfo, waitingForDebugger} = message.params;
        if (targetInfo.type === "page") {
          pages.set(targetInfo.targetId, sessionId);
          await pipe.send("Fetch.enable", {patterns: [{urlPattern: "https://x.com/*", requestStage: "Request"}]}, sessionId);
        }
        if (waitingForDebugger) await pipe.send("Runtime.runIfWaitingForDebugger", {}, sessionId);
      } else if (message.method === "Fetch.requestPaused") {
        xRequests++;
        await pipe.send("Fetch.fulfillRequest", {requestId: message.params.requestId, responseCode: 200,
          responseHeaders: [{name: "Content-Type", value: "text/html; charset=utf-8"}],
          body: Buffer.from(X_PAGE, "utf8").toString("base64")}, message.sessionId);
      }
    } catch { /* A tab that closed while being set up. */ }
  });
  let finished = false;
  const finish = async () => {
    if (finished) return; finished = true; clearTimeout(timer);
    result.xRequests = xRequests;
    try { await Promise.race([pipe.send("Browser.close"), wait(3000)]); } catch {}
    setTimeout(() => browser.kill(), 500);
    process.stdout.write(JSON.stringify(result) + "\n");
  };
  const timer = setTimeout(() => { result.error = result.error || "timeout"; finish(); }, 60000);
  try {
    result.browser = (await pipe.send("Browser.getVersion")).product;
    result.extensionId = (await pipe.send("Extensions.loadUnpacked", {path: extensionPath})).id;
    await pipe.send("Storage.setCookies", {cookies: [
      {name: "auth_token", value: "synthetic-bridge-token", domain: ".x.com", path: "/", secure: true, httpOnly: true},
      {name: "twid", value: "u%3D123456789", domain: ".x.com", path: "/", secure: true}]});
    await pipe.send("Target.setAutoAttach", {autoAttach: true, waitForDebuggerOnStart: true, flatten: true,
      filter: [{type: "page", exclude: false}]});
    const first = (await pipe.send("Target.getTargets")).targetInfos.find(info => info.type === "page");
    let session;
    for (let i = 0; !session && i < 50; i++) { session = pages.get(first.targetId); if (!session) await wait(100); }
    if (!session) throw new Error("no page session");
    const evaluate = async expression =>
      (await pipe.send("Runtime.evaluate", {expression, returnByValue: true}, session)).result.value;
    const until = async (expression, label, limit = 30000) => {
      const end = Date.now() + limit;
      while (Date.now() < end) {
        try { const value = await evaluate(expression); if (value) return value; } catch {}
        await wait(100);
      }
      throw new Error(`waited for ${label}`);
    };
    const shot = async name => {
      if (shotsDir === "-") return;
      fs.mkdirSync(shotsDir, {recursive: true});
      const {data} = await pipe.send("Page.captureScreenshot", {format: "png", captureBeyondViewport: true}, session);
      fs.writeFileSync(path.join(shotsDir, name), Buffer.from(data, "base64"));
    };
    const click = async selector => {
      const point = await evaluate(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()`);
      for (const type of ["mousePressed", "mouseReleased"])
        await pipe.send("Input.dispatchMouseEvent", {type, x: point.x, y: point.y, button: "left", clickCount: 1}, session);
    };
    await pipe.send("Page.enable", {}, session);
    await pipe.send("Emulation.setDeviceMetricsOverride", {width: 900, height: 1000, deviceScaleFactor: 1, mobile: false}, session);
    await pipe.send("Page.navigate", {url: bridgeUrl}, session);
    result.page = await until("location.protocol.endsWith('-extension:') && location.pathname === '/connect.html' && location.href", "consent page");
    result.code = await until("(() => { const c = document.getElementById('pairing-code').textContent.trim(); return c.length === 8 ? c : ''; })()", "pairing code");
    result.stateBefore = await evaluate("document.getElementById('card').dataset.state");
    await shot("1-code.png");
    const started = Date.now();
    await click("#confirm-code");
    await until("!document.getElementById('connect').disabled", "connect button");
    result.checkSeconds = (Date.now() - started) / 1000;
    result.stateReady = await evaluate("document.getElementById('card').dataset.state");
    result.accountName = await evaluate("document.getElementById('account-name').textContent");
    result.accountHandle = await evaluate("document.getElementById('account-handle').textContent");
    result.connectLabel = await evaluate("document.getElementById('connect').textContent");
    result.pagesAfterCheck = (await pipe.send("Target.getTargets")).targetInfos.filter(info => info.type === "page").length;
    await shot("2-account.png");
    await click("#connect");
    result.finalStatus = await until("(() => { const t = document.getElementById('status').textContent; return t.includes('전달했습니다') ? t : ''; })()", "hand-over", 15000);
    result.stateDone = await evaluate("document.getElementById('card').dataset.state");
    await shot("3-done.png");
    // Keep a blank tab so the browser stays open when the consent tab closes.
    await pipe.send("Target.createTarget", {url: "about:blank", background: true});
    await click("#close");
    for (let i = 0; i < 30; i++) {
      const open = (await pipe.send("Target.getTargets")).targetInfos.map(info => info.targetId);
      result.consentTabClosed = !open.includes(first.targetId);
      if (result.consentTabClosed) break;
      await wait(100);
    }
    result.ok = true;
  } catch (error) {
    result.error = error.message;
  }
  await finish();
})();
