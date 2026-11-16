/* Synthetic API boundary checks plus the *production* WebCrypto encryptor. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const {webcrypto} = require("node:crypto");

(async () => {
  let listener, requests = [], cookiesRead = 0, created = 0, closed = 0;
  const extensionId = "abcdefghijklmnopabcdefghijklmnop";
  const extensionUrl = `chrome-extension://${extensionId}/connect.html`;
  const token = "synthetic-bridge-token";
  let currentToken = token, signedIn = true, refused = false, checkTabClosed = false, refuseAfterInspect = false;
  const updates = [];
  const tabs = new Map([[1, {id: 1, url: "http://127.0.0.1:49123/connect"}]]);
  const api = {
    runtime: {id: extensionId, getURL: file => `chrome-extension://${extensionId}/${file}`,
      onMessage: {addListener: callback => { listener = callback; }}},
    tabs: {
      get: async id => { if (!tabs.has(id)) throw new Error(`No tab with id: ${id}.`); return tabs.get(id); },
      update: async (id, changes) => { updates.push([id, changes]); Object.assign(tabs.get(id), changes); },
      // The check tab opens in front; checkTabClosed acts as if the person closed it at once.
      create: async ({url, active}) => { created++; const tab = {id: 2, url, active};
        if (!checkTabClosed) tabs.set(2, tab); return tab; },
      remove: async id => { closed++; tabs.delete(id); },
      sendMessage: async () => { if (refuseAfterInspect) refused = true;
        return {ready: true, username: "Offline_User", display_name: "Offline Name"}; },
      onRemoved: {addListener: () => {}}
    },
    cookies: {
      getAllCookieStores: async () => [{id: "0", tabIds: [1, 2]}],
      get: async ({name}) => { cookiesRead++; if (!signedIn) return null;
        return {name, value: name === "twid" ? "u%3D123456789" : currentToken,
        domain: ".x.com", path: "/", secure: true, httpOnly: true}; }
    }
  };
  const sandbox = {chrome: api, URL, Map, Date, TextEncoder, Uint8Array, crypto: webcrypto, btoa,
    AbortSignal, setTimeout: () => {}, fetch: async (url, options) => {
      // A closed listener refuses the connection; fetch rejects with a TypeError.
      if (refused) throw new TypeError("Failed to fetch");
      requests.push({url, options}); return {ok: true, status: 200, json: async () => ({ok: true})};
    }};
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(process.argv[2], "background.js"), "utf8"), sandbox);
  const send = (message, sender) => new Promise(resolve => listener(message, sender, resolve));
  const local = {id: extensionId, frameId: 0, tab: {id: 1}, url: "http://127.0.0.1:49123/connect"};
  // Extension pages may omit sender.tab and sender.frameId; API tab ID is checked.
  const owned = {id: extensionId, url: extensionUrl};
  const secret = "a".repeat(43);
  assert.equal((await send({type: "luvbox-pair", secret}, {...local, frameId: 1})).ok, false);
  assert.equal((await send({type: "luvbox-pair", secret}, local)).ok, true);
  assert.equal(tabs.get(1).url, extensionUrl);
  assert.equal(cookiesRead, 0, "pairing alone must not read a cookie");
  assert.equal((await send({type: "luvbox-inspect", tabId: 1}, local)).ok, false);
  assert.equal((await send({type: "luvbox-inspect", tabId: 1}, {...owned, frameId: 1})).ok, false);
  const sessions = () => requests.filter(request => request.url.endsWith("/session"));
  assert.equal(requests.length, 1, "only extension readiness is reported before approval");
  assert.equal(requests[0].url, "http://127.0.0.1:49123/progress");
  const status = await send({type: "luvbox-status", tabId: 1}, owned);
  assert.equal(status.code, "AAAAAAAA");
  const inspected = await send({type: "luvbox-inspect", tabId: 1}, owned);
  assert.equal(inspected.ok, true);
  assert.equal(inspected.username, "Offline_User");
  assert.equal(JSON.stringify(inspected).includes(token), false, "cookie must not enter UI reply");
  assert.equal(created, 1); assert.equal(closed, 1);
  assert.equal((await send({type: "luvbox-connect", tabId: 1}, owned)).ok, false);
  assert.equal(sessions().length, 0, "second explicit approval is required");
  currentToken = "changed-account-cookie";
  const changed = await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned);
  assert.equal(changed.ok, false);
  assert.equal(changed.code, "account_changed");
  assert.equal(sessions().length, 0, "account changes must invalidate captured identity");
  signedIn = false;
  const loggedOut = await send({type: "luvbox-inspect", tabId: 1}, owned);
  assert.equal(loggedOut.ok, false);
  assert.equal(loggedOut.code, "login_required");
  signedIn = true;
  currentToken = token;
  assert.equal((await send({type: "luvbox-inspect", tabId: 1}, owned)).ok, true);
  assert.equal((await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned)).ok, true);
  assert.equal(sessions().length, 1);
  assert.equal(JSON.stringify(requests).includes(token), false);
  assert.equal(JSON.stringify(requests).includes(secret), false);
  assert.equal(requests[0].options.redirect, "error");
  assert.equal((await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned)).ok, false);

  // The production encryption function is shared with Python for a true
  // independent AES-GCM/HMAC roundtrip across WebCrypto and gallery_dl.aes.
  const envelope = await sandbox.encryptSession({port: 49123, secret: "offline-pairing-secret-for-webcrypto",
    session: {version: 1, auth_token: token, username: "Offline_User", user_id: "123456789", display_name: "Offline Name"}});
  const progress = await sandbox.encryptPayload({port: 49123, secret: "offline-pairing-secret-for-webcrypto"},
    {version: 1, stage: "extension_ready", sequence: 1}, "/progress");
  // Production progress messages are independently decrypted with WebCrypto
  // here; the emitted fixture is also decrypted by the Python test.
  const stages = [];
  for (const request of requests.filter(item => item.url.endsWith("/progress"))) {
    const derive = purpose => webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(
      `LuvBox browser bridge ${purpose} v1\0${request.url}\0${secret}`));
    const encryptionKey = await webcrypto.subtle.importKey("raw", await derive("encryption"), "AES-GCM", false, ["decrypt"]);
    const wire = JSON.parse(request.options.body);
    const raw = await webcrypto.subtle.decrypt({name: "AES-GCM", iv: Buffer.from(wire.nonce, "base64url"), tagLength: 128},
      encryptionKey, Buffer.from(wire.ciphertext, "base64url"));
    const payload = JSON.parse(new TextDecoder().decode(raw));
    assert.deepEqual(Object.keys(payload).sort(), ["sequence", "stage", "version"]);
    assert.equal(payload.sequence, stages.length + 1);
    stages.push(payload.stage);
  }
  assert.deepEqual(stages, ["extension_ready", "checking_account", "awaiting_approval", "account_changed",
    "checking_account", "login_required", "checking_account", "awaiting_approval"]);
  // The check tab was in front when it closed, so the consent tab came back to the front.
  assert.ok(updates.some(([id, changes]) => id === 1 && changes.active === true), "consent tab re-activated");

  // The app stopped listening (cancelled, closed or timed out): say so, end the
  // pairing, and do not open an X tab for nothing.
  const pairAgain = async () => {
    tabs.get(1).url = "http://127.0.0.1:49123/connect";
    assert.equal((await send({type: "luvbox-pair", secret}, local)).ok, true);
  };
  await pairAgain();
  refused = true;
  const openedBefore = created;
  const gone = await send({type: "luvbox-inspect", tabId: 1}, owned);
  assert.equal(gone.ok, false);
  assert.equal(gone.code, "app_gone");
  assert.equal(created, openedBefore, "no X tab for an app that is gone");
  assert.equal((await send({type: "luvbox-status", tabId: 1}, owned)).code, "expired", "pairing ended");
  refused = false;
  await pairAgain();
  assert.equal((await send({type: "luvbox-inspect", tabId: 1}, owned)).ok, true);
  refused = true;
  const goneAtConnect = await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned);
  assert.equal(goneAtConnect.code, "app_gone");
  assert.equal((await send({type: "luvbox-status", tabId: 1}, owned)).code, "expired", "pairing ended");
  refused = false;

  await pairAgain();
  refuseAfterInspect = true;
  const goneAfterInspect = await send({type: "luvbox-inspect", tabId: 1}, owned);
  assert.equal(goneAfterInspect.code, "app_gone");
  assert.equal((await send({type: "luvbox-status", tabId: 1}, owned)).code, "expired", "pairing deleted when gone after inspect");
  refuseAfterInspect = false;
  refused = false;

  // The person closed the check tab: stop at once instead of waiting 25 s.
  await pairAgain();
  checkTabClosed = true;
  const closedTab = await send({type: "luvbox-inspect", tabId: 1}, owned);
  assert.equal(closedTab.code, "tab_closed");
  checkTabClosed = false;
  assert.equal((await send({type: "luvbox-inspect", tabId: 1}, owned)).ok, true, "a new check works");

  // Programmatic DOM clicks cannot approve even in the extension-owned page.
  const elements = new Map();
  for (const id of ["card", "status", "inspect", "account", "account-hint", "account-name", "account-handle",
    "connect", "close", "confirm-code", "pairing-code"]) {
    // Hidden as connect.html starts them.
    const hidden = ["inspect", "account-name", "account-handle", "close"].includes(id);
    elements.set(id, {checked: false, hidden, dataset: {}, addEventListener(type, callback) { this[type] = callback; }});
  }
  const uiMessages = [];
  const removed = [];
  const uiApi = {tabs: {getCurrent: async () => ({id: 1}), remove: async id => { removed.push(id); }},
    runtime: {sendMessage: async message => {
    uiMessages.push(message);
    if (message.type === "luvbox-status") return {ok: true, code: "AAAAAAAA"};
    return {ok: true, username: "Offline_User"};
  }}};
  const window = {addEventListener: () => {}}; window.top = window;
  const ui = {window, chrome: uiApi, setInterval: () => 0, clearInterval: () => {},
    document: {getElementById: id => elements.get(id)}};
  vm.createContext(ui);
  vm.runInContext(fs.readFileSync(path.join(process.argv[2], "extension-page.js"), "utf8"), ui);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get("card").dataset.state, "code", "the code step is lit once the code is shown");
  assert.equal(elements.get("pairing-code").textContent, "AAAAAAAA");
  assert.equal(elements.get("connect").disabled, true);
  elements.get("confirm-code").checked = true;
  await elements.get("confirm-code").change({isTrusted: false});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-inspect").length, 0);
  await elements.get("inspect").click({isTrusted: false});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-inspect").length, 0);
  await elements.get("confirm-code").change({isTrusted: true});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-inspect").length, 1, "trusted code confirmation checks account automatically");
  assert.equal(elements.get("card").dataset.state, "ready");
  assert.equal(elements.get("account-handle").textContent, "@Offline_User");
  assert.equal(elements.get("account-name").hidden, true, "no display name, no empty name line");
  assert.equal(elements.get("inspect").hidden, false, "re-check appears after the first check");
  assert.equal(elements.get("connect").textContent, "@Offline_User 연결");
  assert.equal(elements.get("connect").disabled, false);
  await elements.get("inspect").click({isTrusted: true});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-inspect").length, 2);
  await elements.get("connect").click({isTrusted: false});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-connect").length, 0);
  await elements.get("close").click({isTrusted: true});
  assert.equal(removed.length, 0, "a hidden close button does nothing before the hand-over");
  await elements.get("connect").click({isTrusted: true});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-connect").length, 1);
  assert.equal(elements.get("card").dataset.state, "done");
  assert.equal(elements.get("connect").hidden, true);
  assert.equal(elements.get("close").hidden, false);
  await elements.get("close").click({isTrusted: false});
  assert.equal(removed.length, 0, "a scripted click cannot close the tab");
  await elements.get("close").click({isTrusted: true});
  assert.deepEqual(removed, [1]);

  // A code that ends the pairing ends the page too: no re-check loop.
  for (const code of ["app_gone", "expired"]) {
    const ended = new Map();
    for (const id of elements.keys()) {
      const hidden = ["inspect", "account-name", "account-handle", "close"].includes(id);
      ended.set(id, {checked: false, hidden, dataset: {}, addEventListener(type, callback) { this[type] = callback; }});
    }
    const endApi = {tabs: {getCurrent: async () => ({id: 1})}, runtime: {sendMessage: async message =>
      message.type === "luvbox-status" ? {ok: true, code: "AAAAAAAA"} : {ok: false, code}}};
    const endUi = {window, chrome: endApi, setInterval: () => 0, clearInterval: () => {},
      document: {getElementById: id => ended.get(id)}};
    vm.createContext(endUi);
    vm.runInContext(fs.readFileSync(path.join(process.argv[2], "extension-page.js"), "utf8"), endUi);
    await new Promise(resolve => setImmediate(resolve));
    ended.get("confirm-code").checked = true;
    await ended.get("confirm-code").change({isTrusted: true});
    assert.equal(ended.get("card").dataset.state, "expired", code);
    assert.equal(ended.get("confirm-code").disabled, true, code);
    assert.equal(ended.get("inspect").hidden, true, code);
    assert.equal(ended.get("connect").disabled, true, code);
  }

  // Unchecking the code confirmation while an inspect call fails returns to the code step cleanly.
  const unticked = new Map();
  for (const id of elements.keys()) {
    const hidden = ["inspect", "account-name", "account-handle", "close"].includes(id);
    unticked.set(id, {checked: false, hidden, dataset: {}, addEventListener(type, callback) { this[type] = callback; }});
  }
  const untickedApi = {tabs: {getCurrent: async () => ({id: 1})}, runtime: {sendMessage: async message => {
    if (message.type === "luvbox-status") return {ok: true, code: "AAAAAAAA"};
    unticked.get("confirm-code").checked = false;
    return {ok: false, code: "no_account"};
  }}};
  const untickedUi = {window, chrome: untickedApi, setInterval: () => 0, clearInterval: () => {},
    document: {getElementById: id => unticked.get(id)}};
  vm.createContext(untickedUi);
  vm.runInContext(fs.readFileSync(path.join(process.argv[2], "extension-page.js"), "utf8"), untickedUi);
  await new Promise(resolve => setImmediate(resolve));
  unticked.get("confirm-code").checked = true;
  await unticked.get("confirm-code").change({isTrusted: true});
  assert.equal(unticked.get("card").dataset.state, "code");
  assert.equal(unticked.get("inspect").hidden, true);
  process.stdout.write(JSON.stringify({session: envelope, progress}));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
