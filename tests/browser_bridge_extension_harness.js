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
  let currentToken = token;
  const tabs = new Map([[1, {id: 1, url: "http://127.0.0.1:49123/connect"}]]);
  const api = {
    runtime: {id: extensionId, getURL: file => `chrome-extension://${extensionId}/${file}`,
      onMessage: {addListener: callback => { listener = callback; }}},
    tabs: {
      get: async id => tabs.get(id),
      update: async (id, changes) => { Object.assign(tabs.get(id), changes); },
      create: async ({url}) => { created++; const tab = {id: 2, url}; tabs.set(2, tab); return tab; },
      remove: async id => { closed++; tabs.delete(id); },
      sendMessage: async () => ({ready: true, username: "Offline_User", display_name: "Offline Name"}),
      onRemoved: {addListener: () => {}}
    },
    cookies: {
      getAllCookieStores: async () => [{id: "0", tabIds: [1, 2]}],
      get: async ({name}) => { cookiesRead++; return {name, value: name === "twid" ? "u%3D123456789" : currentToken,
        domain: ".x.com", path: "/", secure: true, httpOnly: true}; }
    }
  };
  const sandbox = {chrome: api, URL, Map, Date, TextEncoder, Uint8Array, crypto: webcrypto, btoa,
    AbortSignal, setTimeout: () => {}, fetch: async (url, options) => {
      requests.push({url, options}); return {ok: true, json: async () => ({ok: true})};
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
  const status = await send({type: "luvbox-status", tabId: 1}, owned);
  assert.equal(status.code, "AAAAAAAA");
  const inspected = await send({type: "luvbox-inspect", tabId: 1}, owned);
  assert.equal(inspected.ok, true);
  assert.equal(inspected.username, "Offline_User");
  assert.equal(JSON.stringify(inspected).includes(token), false, "cookie must not enter UI reply");
  assert.equal(created, 1); assert.equal(closed, 1);
  assert.equal((await send({type: "luvbox-connect", tabId: 1}, owned)).ok, false);
  assert.equal(requests.length, 0, "second explicit approval is required");
  currentToken = "changed-account-cookie";
  assert.equal((await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned)).ok, false);
  assert.equal(requests.length, 0, "account changes must invalidate captured identity");
  currentToken = token;
  assert.equal((await send({type: "luvbox-inspect", tabId: 1}, owned)).ok, true);
  assert.equal((await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned)).ok, true);
  assert.equal(requests.length, 1);
  assert.equal(JSON.stringify(requests).includes(token), false);
  assert.equal(JSON.stringify(requests).includes(secret), false);
  assert.equal(requests[0].options.redirect, "error");
  assert.equal((await send({type: "luvbox-connect", tabId: 1, confirmed: true}, owned)).ok, false);

  // The production encryption function is shared with Python for a true
  // independent AES-GCM/HMAC roundtrip across WebCrypto and gallery_dl.aes.
  const envelope = await sandbox.encryptSession({port: 49123, secret: "offline-pairing-secret-for-webcrypto",
    session: {version: 1, auth_token: token, username: "Offline_User", user_id: "123456789", display_name: "Offline Name"}});

  // Programmatic DOM clicks cannot approve even in the extension-owned page.
  const elements = new Map();
  for (const id of ["status", "inspect", "account", "connect", "confirm-code", "pairing-code"]) {
    elements.set(id, {checked: false, addEventListener(type, callback) { this[type] = callback; }});
  }
  const uiMessages = [];
  const uiApi = {tabs: {getCurrent: async () => ({id: 1})}, runtime: {sendMessage: async message => {
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
  elements.get("confirm-code").checked = true;
  await elements.get("inspect").click({isTrusted: false});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-inspect").length, 0);
  await elements.get("inspect").click({isTrusted: true});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-inspect").length, 1);
  await elements.get("connect").click({isTrusted: false});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-connect").length, 0);
  await elements.get("connect").click({isTrusted: true});
  assert.equal(uiMessages.filter(x => x.type === "luvbox-connect").length, 1);
  process.stdout.write(JSON.stringify(envelope));
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
