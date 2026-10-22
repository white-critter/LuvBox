/* Extension-owned consent UI. Nothing is exposed as a web-accessible resource. */
(() => {
  "use strict";
  if (window !== window.top) return;
  const api = globalThis.browser || globalThis.chrome;
  const currentTab = api.tabs.getCurrent();
  const send = async message => {
    const tab = await currentTab;
    return api.runtime.sendMessage({...message, tabId: tab.id});
  };
  const status = document.getElementById("status");
  const inspect = document.getElementById("inspect");
  const account = document.getElementById("account");
  const connect = document.getElementById("connect");
  const confirmation = document.getElementById("confirm-code");
  let ready = false;
  let busy = false;
  let heartbeat;
  const update = () => { inspect.disabled = busy || !confirmation.checked; connect.disabled = busy || !ready || !confirmation.checked; };
  const expired = () => {
    clearInterval(heartbeat);
    confirmation.checked = false;
    confirmation.disabled = true;
    ready = false;
    update();
    status.textContent = "연결 시간이 지났거나 확장이 다시 시작되었습니다. 앱에서 브라우저 연결을 다시 시작해 주세요.";
  };
  window.addEventListener("pagehide", () => clearInterval(heartbeat), {once: true});
  confirmation.addEventListener("change", event => { if (event.isTrusted) update(); });
  send({type: "luvbox-status"}).then(result => {
    if (!result?.ok) throw new Error();
    document.getElementById("pairing-code").textContent = result.code;
    status.textContent = "LuvBox 앱과 위 연결 코드가 같은지 확인해 주세요. 계정을 확인할 때 X 탭을 잠시 열고 닫습니다.";
    // MV3 may suspend an idle background worker after 30 seconds. A short,
    // memory-only pairing must survive while the user reads the consent page.
    heartbeat = setInterval(() => send({type: "luvbox-status"})
      .then(reply => { if (!reply?.ok) expired(); }, expired), 20000);
  }).catch(expired);
  inspect.addEventListener("click", async event => {
    if (!event.isTrusted || busy || !confirmation.checked) return;
    busy = true;
    ready = false;
    update();
    connect.hidden = true;
    account.textContent = "";
    status.textContent = "현재 브라우저의 X 계정을 확인하고 있습니다…";
    try {
      const result = await send({type: "luvbox-inspect"});
      if (!result?.ok) throw new Error();
      account.textContent = (result.display_name ? result.display_name + "\n" : "") + "@" + result.username;
      status.textContent = "이 계정의 X 로그인 쿠키를 이 컴퓨터의 LuvBox에 전달합니다. 로그인은 앱에서 해제할 수 있습니다.";
      connect.textContent = "@" + result.username + " 연결";
      connect.hidden = false;
      ready = true;
    } catch {
      status.textContent = "X 계정을 확인하지 못했습니다. 이 브라우저의 일반 창에서 x.com에 로그인한 뒤 다시 확인해 주세요. 앱의 연결 대기 시간이 지났다면 앱에서 다시 시작해 주세요.";
    } finally { busy = false; update(); }
  });
  connect.addEventListener("click", async event => {
    if (!event.isTrusted || busy || !ready || !confirmation.checked) return;
    busy = true;
    update();
    try {
      const result = await send({type: "luvbox-connect", confirmed: true});
      if (!result?.ok) throw new Error();
      clearInterval(heartbeat);
      status.textContent = "계정 정보를 전달했습니다. LuvBox에서 연결 결과를 확인하세요. 이 탭은 닫아도 됩니다.";
      connect.hidden = true;
      inspect.hidden = true;
      confirmation.disabled = true;
    } catch {
      status.textContent = "연결을 완료하지 못했습니다. 앱에서 연결을 다시 시작해 주세요.";
      connect.hidden = true;
    } finally { busy = false; ready = false; update(); }
  });
})();
