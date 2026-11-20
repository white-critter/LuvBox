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
  const element = id => document.getElementById(id);
  const card = element("card");
  const status = element("status");
  const inspect = element("inspect");
  const accountHint = element("account-hint");
  const accountName = element("account-name");
  const accountHandle = element("account-handle");
  const connect = element("connect");
  const close = element("close");
  const confirmation = element("confirm-code");
  let ready = false;
  let busy = false;
  let finished = false;
  let heartbeat;
  // One state word drives the page's look: which step is lit, the status tone.
  const show = (state, text) => {
    card.dataset.state = state;
    status.textContent = text;
  };
  const failureText = code => ({
    login_required: "이 브라우저 프로필에서 X에 로그인하거나 추가 인증을 마친 뒤 ‘계정 다시 확인’을 누르세요.",
    account_changed: "확인 중 X 계정이 바뀌었습니다. 연결할 계정으로 전환한 뒤 ‘계정 다시 확인’을 누르세요.",
    account_error: "X 계정을 읽지 못했습니다. X 페이지가 정상적으로 열리는지 확인한 뒤 ‘계정 다시 확인’을 누르세요.",
    tab_closed: "확인용 X 탭이 닫혀 계정을 확인하지 못했습니다. ‘계정 다시 확인’을 누르세요.",
    expired: "연결 시간이 지났습니다. LuvBox에서 연결을 다시 시작해 주세요.",
    app_gone: "LuvBox 앱이 이 연결을 더 이상 기다리지 않습니다. 이 탭을 닫고 앱에서 연결을 다시 시작해 주세요."
  })[code] || "연결을 완료하지 못했습니다. LuvBox의 연결 상태를 확인한 뒤 다시 시도해 주세요.";
  // These end the pairing; nothing on this page can continue it.
  const final = new Set(["expired", "app_gone"]);
  const codeText = "LuvBox 앱의 연결 코드와 같은지 확인한 뒤 체크하세요.";
  const firstHint = "코드를 확인하면 이 브라우저의 X 계정을 바로 확인합니다. 확인하는 동안 X 탭이 잠시 열렸다 닫힙니다.";
  const showAccount = (name, handle, hint) => {
    accountName.textContent = name;
    accountName.hidden = !name;
    accountHandle.textContent = handle;
    accountHandle.hidden = !handle;
    accountHint.textContent = hint;
    accountHint.hidden = !hint;
  };
  const resetConnect = () => {
    ready = false;
    connect.textContent = "계정 연결";
  };
  const update = () => {
    inspect.disabled = busy || confirmation.disabled || !confirmation.checked;
    connect.disabled = busy || !ready || confirmation.disabled || !confirmation.checked;
  };
  const end = text => {
    if (finished) return;
    finished = true;
    clearInterval(heartbeat);
    confirmation.checked = false;
    confirmation.disabled = true;
    inspect.hidden = true;
    resetConnect();
    update();
    show("expired", text);
  };
  const expired = () => end("연결 시간이 지났거나 확장이 다시 시작되었습니다. 앱에서 브라우저 연결을 다시 시작해 주세요.");
  window.addEventListener("pagehide", () => clearInterval(heartbeat), {once: true});
  confirmation.disabled = true;
  confirmation.addEventListener("change", event => {
    if (!event.isTrusted || confirmation.disabled) return;
    if (!confirmation.checked) {
      resetConnect();
      update();
      if (!busy) showAccount("", "", firstHint);
      return show("code", codeText);
    }
    update();
    return inspectAccount();
  });
  send({type: "luvbox-status"}).then(result => {
    if (finished) return;
    if (!result?.ok) throw new Error();
    element("pairing-code").textContent = result.code;
    confirmation.disabled = false;
    show("code", codeText);
    update();
    // MV3 may suspend an idle background worker after 30 seconds. A short,
    // memory-only pairing must survive while the user reads the consent page.
    heartbeat = setInterval(() => {
      if (finished) return;
      send({type: "luvbox-status"})
        .then(reply => { if (!finished && !reply?.ok) expired(); }, () => { if (!finished) expired(); });
    }, 20000);
  }).catch(() => { if (!finished) expired(); });
  async function inspectAccount() {
    if (finished || busy || confirmation.disabled || !confirmation.checked) return;
    busy = true;
    resetConnect();
    update();
    showAccount("", "", "확인하는 중…");
    show("checking", "이 브라우저의 X 계정을 확인하는 중… X 탭이 잠시 열렸다 닫힙니다.");
    try {
      const result = await send({type: "luvbox-inspect"});
      // The pairing may have ended while the check ran; keep that notice.
      if (finished || confirmation.disabled) return;
      if (!result?.ok && final.has(result?.code)) return end(failureText(result.code));
      // The box was unticked during the check: stay on the code step.
      if (!confirmation.checked) {
        showAccount("", "", firstHint);
        return show("code", codeText);
      }
      if (!result?.ok) {
        showAccount("", "", "계정을 확인하지 못했습니다.");
        show("failed", failureText(result?.code));
        return;
      }
      showAccount(result.display_name || "", "@" + result.username, "");
      connect.textContent = "@" + result.username + " 연결";
      ready = true;
      show("ready", "표시된 계정이 맞으면 아래 연결 버튼을 누르세요. 로그인은 앱에서 언제든 해제할 수 있습니다.");
    } catch {
      if (finished || confirmation.disabled) return;
      showAccount("", "", "계정을 확인하지 못했습니다.");
      show("failed", failureText("account_error"));
    } finally {
      busy = false;
      // The re-check button appears once a first check has run.
      if (!finished && confirmation.checked && !confirmation.disabled) inspect.hidden = false;
      update();
    }
  }
  inspect.addEventListener("click", event => {
    if (event.isTrusted) return inspectAccount();
  });
  connect.addEventListener("click", async event => {
    if (!event.isTrusted || finished || busy || !ready || !confirmation.checked) return;
    busy = true;
    update();
    show("sending", "LuvBox로 전달하는 중…");
    try {
      const result = await send({type: "luvbox-connect", confirmed: true});
      if (finished) return;
      if (!result?.ok) {
        if (final.has(result?.code)) return end(failureText(result.code));
        // Every submission needs a new account check, including a failed one.
        resetConnect();
        showAccount("", "", "‘계정 다시 확인’을 눌러 계정을 다시 확인하세요.");
        show("failed", failureText(result?.code));
        return;
      }
      finished = true;
      clearInterval(heartbeat);
      confirmation.disabled = true;
      inspect.hidden = true;
      connect.hidden = true;
      close.hidden = false;
      ready = false;
      show("done", "계정 정보를 전달했습니다. LuvBox 앱으로 돌아가 ‘이 계정 연결 승인’을 눌러야 저장됩니다(3분 안에). 이 탭은 닫아도 됩니다.");
    } catch {
      if (finished) return;
      resetConnect();
      showAccount("", "", "‘계정 다시 확인’을 눌러 계정을 다시 확인하세요.");
      show("failed", "연결을 완료하지 못했습니다. 앱에서 연결을 다시 시작해 주세요.");
    } finally {
      busy = false;
      update();
    }
  });
  close.addEventListener("click", async event => {
    if (!event.isTrusted || close.hidden) return;
    try {
      await api.tabs.remove((await currentTab).id);
    } catch {
      window.close();
    }
  });
})();
