/* Isolated-world script. It never receives or reads an authentication cookie. */
(() => {
  "use strict";
  const api = globalThis.browser || globalThis.chrome;
  if (location.origin !== "https://x.com") return;
  api.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message?.type !== "luvbox-profile") return false;
    const link = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
    if (!link) { respond({ready: false}); return false; }
    try {
      const profile = new URL(link.href, location.origin);
      const match = /^\/([A-Za-z0-9_]{1,15})\/?$/.exec(profile.pathname);
      if (profile.origin !== "https://x.com" || profile.username || profile.password ||
          profile.search || profile.hash || !match) throw new Error();
      const handle = match[1];
      // Do not derive a name/handle from someone else's viewed profile.
      const switcher = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
      const lines = (switcher?.innerText || "").split("\n").map(x => x.trim()).filter(Boolean);
      const handles = [...(switcher?.textContent || "").matchAll(/@([A-Za-z0-9_]{1,15})/g)].map(x => x[1].toLowerCase());
      if (handles.length && !handles.includes(handle.toLowerCase())) throw new Error();
      const displayName = (lines.find(x => x !== "@" + handle && !x.startsWith("@")) || "")
        .replace(/[\x00-\x1f\x7f-\x9f]/g, "").slice(0, 200);
      respond({ready: true, username: handle, display_name: displayName});
    } catch { respond({ready: false}); }
    return false;
  });
})();
