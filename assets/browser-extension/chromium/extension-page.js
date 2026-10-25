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
  const failureText = code => ({
    login_required: "이 브라우저 프로필에서 X에 로그인하거나 추가 인증을 마친 뒤 ‘계정 다시 확인’을 누르세요.",
    account_changed: "확인 중 X 계정이 바뀌었습니다. 연결할 계정으로 전환한 뒤 ‘계정 다시 확인’을 누르세요.",
    account_error: "X 계정을 읽지 못했습니다. X 페이지가 정상적으로 열리는지 확인한 뒤 ‘계정 다시 확인’을 누르세요.",
    expired: "연결 시간이 지났습니다. LuvBox에서 연결을 다시 시작해 주세요."
  })[code] || "연결을 완료하지 못했습니다. LuvBox의 연결 상태를 확인한 뒤 다시 시도해 주세요.";
  const update = () => {
    inspect.disabled = busy || confirmation.disabled || !confirmation.checked;
    connect.disabled = busy || !ready || confirmation.disabled || !confirmation.checked;
  };
  const expired = () => {
    clearInterval(heartbeat);
    confirmation.checked = false;
    confirmation.disabled = true;
    ready = false;
    update();
    status.textContent = "연결 시간이 지났거나 확장이 다시 시작되었습니다. 앱에서 브라우저 연결을 다시 시작해 주세요.";
  };
  window.addEventListener("pagehide", () => clearInterval(heartbeat), {once: true});
  confirmation.disabled = true;
  confirmation.addEventListener("change", event => {
    if (!event.isTrusted || confirmation.disabled) return;
    update();
    if (confirmation.checked) return inspectAccount();
  });
  send({type: "luvbox-status"}).then(result => {
    if (!result?.ok) throw new Error();
    document.getElementById("pairing-code").textContent = result.code;
    confirmation.disabled = false;
    status.textContent = "LuvBox 앱과 코드가 같은지 확인하고 체크하세요. X 탭을 잠시 열어 계정을 자동 확인합니다.";
    update();
    // MV3 may suspend an idle background worker after 30 seconds. A short,
    // memory-only pairing must survive while the user reads the consent page.
    heartbeat = setInterval(() => send({type: "luvbox-status"})
      .then(reply => { if (!reply?.ok) expired(); }, expired), 20000);
  }).catch(expired);
  async function inspectAccount() {
    if (busy || confirmation.disabled || !confirmation.checked) return;
    busy = true;
    ready = false;
    update();
    connect.hidden = true;
    account.textContent = "";
    status.textContent = "현재 브라우저의 X 계정을 확인하고 있습니다…";
    try {
      const result = await send({type: "luvbox-inspect"});
      if (!result?.ok) {
        status.textContent = failureText(result?.code);
        return;
      }
      account.textContent = (result.display_name ? result.display_name + "\n" : "") + "@" + result.username;
      status.textContent = "이 계정의 X 로그인 쿠키를 이 컴퓨터의 LuvBox에 전달합니다. 로그인은 앱에서 해제할 수 있습니다.";
      connect.textContent = "@" + result.username + " 연결";
      connect.hidden = false;
      ready = true;
    } catch {
      status.textContent = failureText("account_error");
    } finally { busy = false; update(); }
  }
  inspect.addEventListener("click", event => {
    if (event.isTrusted) return inspectAccount();
  });
  connect.addEventListener("click", async event => {
    if (!event.isTrusted || busy || !ready || !confirmation.checked) return;
    busy = true;
    update();
    try {
      const result = await send({type: "luvbox-connect", confirmed: true});
      if (!result?.ok) {
        status.textContent = failureText(result?.code);
        connect.hidden = true;
        return;
      }
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
