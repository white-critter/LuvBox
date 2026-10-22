/* Pairing handoff only. Cookie approval never appears in a localhost page. */
(() => {
  "use strict";
  const api = globalThis.browser || globalThis.chrome;
  const match = /^#luvbox=([A-Za-z0-9_-]{43})$/.exec(location.hash);
  if (window !== window.top || location.protocol !== "http:" || location.hostname !== "127.0.0.1" ||
      !location.port || location.pathname !== "/connect" || location.search || !match) return;
  const secret = match[1];
  history.replaceState(null, "", "/connect");
  api.runtime.sendMessage({type: "luvbox-pair", secret}).catch(() => {});
})();
