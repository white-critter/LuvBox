/* No external messages, storage, analytics, credential logs, or remote requests. */
"use strict";
const api = globalThis.browser || globalThis.chrome;
const pairings = new Map();
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const publicError = "연결하지 못했습니다. X 로그인과 앱의 연결 대기 상태를 확인한 뒤 다시 시도해 주세요.";

function pairingSource(sender) {
  if (sender.frameId !== 0 || !sender.tab || !Number.isInteger(sender.tab.id) || typeof sender.url !== "string") throw new Error();
  const url = new URL(sender.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
      url.pathname !== "/connect" || url.search || url.username || url.password) throw new Error();
  const port = Number(url.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error();
  return {tabId: sender.tab.id, port};
}

async function currentPairing(message, sender) {
  // Extension documents can omit sender.tab/frameId (unlike content scripts).
  // Their tab ID is supplied by tabs.getCurrent(), then checked independently.
  if (sender.id !== api.runtime.id || sender.url !== api.runtime.getURL("connect.html") ||
      (sender.frameId !== undefined && sender.frameId !== 0) || !Number.isInteger(message.tabId)) throw new Error();
  const tab = await api.tabs.get(message.tabId);
  if (tab.url !== api.runtime.getURL("connect.html") || (sender.tab && sender.tab.id !== tab.id)) throw new Error();
  const pairing = pairings.get(tab.id);
  if (!pairing || Date.now() > pairing.expires) {
    pairings.delete(tab.id);
    throw new Error();
  }
  return {pairing, tabId: tab.id};
}

function userId(value) {
  const match = /^u=([1-9][0-9]{0,31})$/.exec(decodeURIComponent(value || "").replace(/^"|"$/g, ""));
  if (!match) throw new Error();
  return match[1];
}

async function sessionCookies(storeId) {
  const details = {url: "https://x.com/"};
  if (storeId !== undefined) details.storeId = storeId;
  const [auth, twid] = await Promise.all([
    api.cookies.get({...details, name: "auth_token"}),
    api.cookies.get({...details, name: "twid"})
  ]);
  if (!auth || !twid || auth.path !== "/" || twid.path !== "/" ||
      !["x.com", ".x.com"].includes(auth.domain) ||
      !["x.com", ".x.com"].includes(twid.domain) ||
      !auth.secure || !auth.httpOnly || !auth.value || auth.value.length > 4096 ||
      /[\x00-\x20\x7f-\uffff",;\\]/.test(auth.value)) throw new Error();
  return {auth_token: auth.value, user_id: userId(twid.value), storeId};
}

function encoded(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function encryptSession(pairing) {
  const encoder = new TextEncoder();
  const endpoint = `http://127.0.0.1:${pairing.port}/session`;
  const derive = purpose => crypto.subtle.digest("SHA-256", encoder.encode(
    `LuvBox browser bridge ${purpose} v1\0${endpoint}\0${pairing.secret}`));
  const encryptionKey = await crypto.subtle.importKey("raw", await derive("encryption"), "AES-GCM", false, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const cipher = new Uint8Array(await crypto.subtle.encrypt({name: "AES-GCM", iv: nonce, tagLength: 128},
    encryptionKey, encoder.encode(JSON.stringify(pairing.session))));
  const proofKey = await crypto.subtle.importKey("raw", await derive("proof"), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  const signed = new Uint8Array(nonce.length + cipher.length);
  signed.set(nonce); signed.set(cipher, nonce.length);
  const proof = new Uint8Array(await crypto.subtle.sign("HMAC", proofKey, signed));
  return {version: 1, nonce: encoded(nonce), ciphertext: encoded(cipher), proof: encoded(proof)};
}

async function inspectAccount(pairing) {
  // Opening a fresh same-profile tab avoids an old tab's stale account handle
  // being associated with the current account cookie after an X account switch.
  // The browser retains its existing login; no profile directory is accessed.
  let tab;
  try {
    const before = await sessionCookies();
    tab = await api.tabs.create({url: "https://x.com/home", active: true});
    const stores = await api.cookies.getAllCookieStores();
    const store = stores.find(candidate => candidate.tabIds.includes(tab.id));
    const storeId = tab.cookieStoreId ?? store?.id;
    const start = await sessionCookies(storeId);
    if (start.auth_token !== before.auth_token || start.user_id !== before.user_id) throw new Error();
    const deadline = Date.now() + 25000;
    let profile;
    while (Date.now() < deadline && Date.now() < pairing.expires) {
      try {
        const current = await api.tabs.get(tab.id);
        if (current.url && new URL(current.url).origin !== "https://x.com") throw new Error();
        profile = await api.tabs.sendMessage(tab.id, {type: "luvbox-profile"});
        if (profile?.ready && /^[A-Za-z0-9_]{1,15}$/.test(profile.username)) break;
      } catch { /* The new tab may still be loading its content script. */ }
      await wait(250);
    }
    if (!profile?.ready) throw new Error();
    const after = await sessionCookies(storeId);
    if (start.auth_token !== after.auth_token || start.user_id !== after.user_id) throw new Error();
    pairing.session = {version: 1, auth_token: after.auth_token, user_id: after.user_id,
      username: profile.username, display_name: profile.display_name || ""};
    pairing.storeId = storeId;
    return {ok: true, username: profile.username, user_id: after.user_id,
      display_name: pairing.session.display_name};
  } finally {
    if (tab?.id !== undefined) {
      try { await api.tabs.remove(tab.id); } catch { /* Already closed by user. */ }
    }
  }
}

async function dispatch(message, sender) {
  if (!message || typeof message.type !== "string") return undefined;
  if (message.type === "luvbox-pair") {
    const {tabId, port} = pairingSource(sender);
    if (typeof message.secret !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(message.secret)) throw new Error();
    const pairing = {port, secret: message.secret, expires: Date.now() + 180000};
    pairings.set(tabId, pairing);
    setTimeout(() => { if (pairings.get(tabId) === pairing) pairings.delete(tabId); }, 180000);
    // Consent belongs to an extension-owned page. A localhost page must never
    // be able to call DOM .click() or overlay the credential approval buttons.
    await api.tabs.update(tabId, {url: api.runtime.getURL("connect.html")});
    return {ok: true};
  }
  const {pairing, tabId} = await currentPairing(message, sender);
  if (message.type === "luvbox-status") return {ok: true, code: pairing.secret.slice(0, 8).toUpperCase()};
  if (message.type === "luvbox-inspect") {
    if (pairing.busy) throw new Error();
    pairing.busy = true;
    pairing.session = undefined;
    try { return await inspectAccount(pairing); }
    finally { pairing.busy = false; }
  }
  if (message.type === "luvbox-connect") {
    if (message.confirmed !== true || pairing.busy || !pairing.session) throw new Error();
    pairing.busy = true;
    try {
      const current = await sessionCookies(pairing.storeId);
      if (current.auth_token !== pairing.session.auth_token || current.user_id !== pairing.session.user_id) throw new Error();
      if (Date.now() > pairing.expires) throw new Error();
      const envelope = await encryptSession(pairing);
      const response = await fetch(`http://127.0.0.1:${pairing.port}/session`, {
        method: "POST", mode: "cors", cache: "no-store", credentials: "omit", redirect: "error",
        referrerPolicy: "no-referrer", signal: AbortSignal.timeout(5000),
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(envelope)
      });
      if (!response.ok || !(await response.json()).ok) throw new Error();
      pairings.delete(tabId);
      return {ok: true};
    } finally {
      // Every submission requires a new account check, including a failed one.
      pairing.session = undefined;
      pairing.busy = false;
    }
  }
  throw new Error();
}

api.runtime.onMessage.addListener((message, sender, respond) => {
  dispatch(message, sender).then(respond, () => respond({ok: false, error: publicError}));
  return true;
});
api.tabs.onRemoved.addListener(tabId => pairings.delete(tabId));
