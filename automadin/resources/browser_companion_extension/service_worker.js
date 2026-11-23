const HOST = "com.automadin.browser_companion";
let nativePort = null;
let granted = null;
let nextLoadEpoch = 0;
const pendingLoads = new Map();
const completedLoads = new Map();
const loadErrors = new Map();
const CONTENT_REPLY_TIMEOUT_MS = 3000;
const MAX_CONTENT_REPLY_TIMEOUT_MS = 120000;
function post(message) { if (nativePort) nativePort.postMessage(message); }
function contentReplyTimeout(request) {
  const p = request.params || {};
  let operationMs = 0;
  if (request.op === "fill") {
    operationMs = String(p.value ?? "").length * Math.max(0, Number(p.max_delay || 0));
  } else if (request.op === "type") {
    operationMs = String(p.text ?? "").length * Math.max(0, Number(p.max_delay_ms || 0));
  } else if (request.op === "press") {
    const count = Array.isArray(p.keys) ? p.keys.length : 0;
    operationMs = Math.max(0, count - 1) * Math.max(0, Number(p.pause_ms || 0));
  } else if (request.op === "click") {
    operationMs = Math.max(0, Number(p.hold_duration_ms || 0));
  }
  return Math.min(
    MAX_CONTENT_REPLY_TIMEOUT_MS,
    CONTENT_REPLY_TIMEOUT_MS + operationMs,
  );
}
function bounded(promise, message, timeoutMs = CONTENT_REPLY_TIMEOUT_MS) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}
async function sendContent(tabId, request) {
  return await bounded(
    chrome.tabs.sendMessage(tabId, request),
    `Companion content script timed out while handling ${request.op}`,
    contentReplyTimeout(request),
  );
}
async function requestContent(tabId, request) {
  let reply;
  try {
    reply = await sendContent(tabId, request);
  } catch (error) {
    // Visibility probes are read-only, so replacing a missing/stalled receiver
    // and retrying is safe. Never replay clicks or text entry: their outcome is
    // unknown when a response is lost.
    if (request.op !== "visible") throw error;
    await bounded(
      chrome.scripting.executeScript({target: {tabId}, files: ["content.js"]}),
      "Companion content script reinjection timed out",
    );
    reply = await sendContent(tabId, request);
  }
  if (!reply || !reply.ok) {
    throw new Error(reply?.error || "Companion content script did not respond");
  }
  return reply.result;
}
async function setActionState(tabId, text, color, title) {
  await chrome.action.setBadgeText({tabId, text});
  if (text) await chrome.action.setBadgeBackgroundColor({tabId, color});
  await chrome.action.setTitle({tabId, title});
}
async function summary(tab) {
  return {target_id: `${tab.windowId}:${tab.id}`, tab_id: tab.id,
    window_id: tab.windowId, title: tab.title || "", url: tab.url || "",
    active: Boolean(tab.active)};
}
async function saveGrant(value) {
  granted = value;
  if (value) await chrome.storage.session.set({granted: value});
  else await chrome.storage.session.remove("granted");
}
async function setGranted(tab) {
  await saveGrant(await summary(tab));
  post({type: "granted", ...granted});
}
async function revokeGrant() {
  const previous = granted;
  await saveGrant(null);
  if (previous) post({type: "revoked", target_id: previous.target_id});
}
async function restoreGrant() {
  let saved = (await chrome.storage.session.get("granted")).granted || null;
  // Migrate an ON badge left by an earlier extension build whose worker was suspended.
  if (!saved) {
    const tabs = await chrome.tabs.query({});
    const marked = [];
    for (const tab of tabs) {
      if (tab.id && await chrome.action.getBadgeText({tabId: tab.id}) === "ON") marked.push(tab);
    }
    if (marked.length === 1) saved = await summary(marked[0]);
  }
  if (!saved?.tab_id) return;
  try {
    const tab = await chrome.tabs.get(saved.tab_id);
    if (!tab.url || !/^https?:/.test(tab.url) || `${tab.windowId}:${tab.id}` !== saved.target_id) {
      await saveGrant(null); return;
    }
    await saveGrant(await summary(tab));
  } catch (_) { await saveGrant(null); }
}
async function connect() {
  try {
    await restoreGrant();
    nativePort = chrome.runtime.connectNative(HOST);
    nativePort.onMessage.addListener(handleNative);
    nativePort.onDisconnect.addListener(async () => {
      nativePort = null;
      if (granted?.tab_id) {
        await setActionState(granted.tab_id, "WAIT", "#b26a00",
          "Automadin is closed or disconnected. Click to reconnect this tab.");
      }
    });
    post({type: "hello", protocol: 4, extension_id: chrome.runtime.id,
      user_agent: navigator.userAgent});
    if (granted) post({type: "granted", ...granted});
  } catch (_) { nativePort = null; }
}
chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id) return;
  if (!tab.url || !/^https?:/.test(tab.url)) {
    await setActionState(tab.id, "WEB", "#b26a00",
      "Open a normal http(s) website first; Edge/Chrome do not allow companions on New Tab or browser pages.");
    return;
  }
  if (granted?.tab_id === tab.id && nativePort) {
    await chrome.action.setBadgeText({tabId: tab.id, text: ""});
    await revokeGrant();
    return;
  }
  if (granted?.tab_id) {
    await chrome.action.setBadgeText({tabId: granted.tab_id, text: ""});
  }
  await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["content.js"]});
  await saveGrant(await summary(tab));
  if (!nativePort) await connect();
  await setActionState(tab.id, nativePort ? "ON" : "WAIT",
    nativePort ? "#2e7d32" : "#b26a00",
    nativePort ? "Connected to Automadin. Click to disconnect this tab." :
      "Automadin is closed or disconnected. Click to reconnect this tab.");
  post({type: "granted", ...granted});
});
chrome.tabs.onRemoved.addListener(async (tabId) => {
  if (granted && granted.tab_id === tabId) {
    await revokeGrant();
  }
});
chrome.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if (!granted || granted.tab_id !== tabId) return;
  if (change.status === "complete" && tab.url && /^https?:/.test(tab.url)) {
    const epoch = pendingLoads.get(tabId) || 0;
    try {
      await chrome.scripting.executeScript({target: {tabId}, files: ["content.js"]});
      loadErrors.delete(tabId);
      completedLoads.set(tabId, epoch);
    } catch (error) {
      loadErrors.set(tabId, {epoch, message: String(error?.message || error)});
    }
  }
  await setGranted(tab);
});
async function handleNative(request) {
  if (!request || request.type !== "request") return;
  const response = {type: "response", id: request.id};
  try {
    if (!granted) throw new Error("No browser tab is granted to Automadin");
    const p = request.params || {};
    if (request.op === "tabs_snapshot") {
      const list = await chrome.tabs.query({windowId: granted.window_id});
      response.result = {tabs: await Promise.all(list.map(summary)), active_target_id: granted.target_id};
    } else if (request.op === "select_tab") {
      const tabId = Number(String(p.target_id).split(":")[1]);
      await chrome.tabs.update(tabId, {active: true});
      await setGranted(await chrome.tabs.get(tabId)); response.result = null;
    } else if (request.op === "open_tab") {
      const tab = await chrome.tabs.create({windowId: granted.window_id, url: p.url, active: true});
      await setGranted(tab); response.result = granted.target_id;
    } else if (request.op === "close_tab") {
      const closed = granted.target_id;
      const tabs = await chrome.tabs.query({windowId: granted.window_id});
      if (tabs.length === 1) {
        const replacement = await chrome.tabs.create({windowId: granted.window_id, url: "about:blank", active: true});
        await setGranted(replacement); await chrome.tabs.remove(Number(closed.split(":")[1]));
        response.result = {closed_target_id: closed, opened_replacement: true};
      } else {
        await chrome.tabs.remove(granted.tab_id);
        const active = (await chrome.tabs.query({windowId: granted.window_id, active: true}))[0];
        await setGranted(active); response.result = {closed_target_id: closed, opened_replacement: false};
      }
    } else if (request.op === "close_all_tabs") {
      const tabs = await chrome.tabs.query({windowId: granted.window_id});
      const replacement = await chrome.tabs.create({windowId: granted.window_id, url: "about:blank", active: true});
      await chrome.tabs.remove(tabs.map(t => t.id)); await setGranted(replacement);
      response.result = granted.target_id;
    } else if (request.op === "switch_tab") {
      const tabs = await chrome.tabs.query({windowId: granted.window_id});
      const tab = tabs[Number(p.index)]; if (!tab) throw new Error("Browser tab index is out of range");
      await chrome.tabs.update(tab.id, {active: true}); await setGranted(await chrome.tabs.get(tab.id));
      response.result = granted.target_id;
    } else if (request.op === "select_page") {
      const tabs = await chrome.tabs.query({windowId: granted.window_id});
      const strategy = String(p.strategy || "").toLowerCase(); let candidates = [];
      if (strategy === "newest") candidates = tabs.slice(-1);
      else if (["url_exact","url_contains","title_exact","title_contains"].includes(strategy)) {
        const field = strategy.startsWith("url") ? "url" : "title";
        const exact = strategy.endsWith("exact"); const needle = p.match_case ? String(p.value) : String(p.value).toLowerCase();
        candidates = tabs.filter(t => { const value = p.match_case ? String(t[field] || "") : String(t[field] || "").toLowerCase(); return exact ? value === needle : value.includes(needle); });
      } else throw new Error(`Unsupported companion page strategy: ${strategy}`);
      if (candidates.length !== 1) throw new Error(`Browser page rule matched ${candidates.length} tabs`);
      await chrome.tabs.update(candidates[0].id, {active: true}); await setGranted(await chrome.tabs.get(candidates[0].id)); response.result = granted.target_id;
    } else if (request.op === "ensure_page") {
      response.result = {target_id: granted.target_id, created: false};
    } else if (request.op === "navigate") {
      const epoch = ++nextLoadEpoch; pendingLoads.set(granted.tab_id, epoch);
      await chrome.tabs.update(granted.tab_id, {url: p.url}); response.result = epoch;
    } else if (request.op === "reload") {
      const epoch = ++nextLoadEpoch; pendingLoads.set(granted.tab_id, epoch);
      await chrome.tabs.reload(granted.tab_id); response.result = epoch;
    } else if (request.op === "back" || request.op === "forward") {
      const epoch = ++nextLoadEpoch; pendingLoads.set(granted.tab_id, epoch);
      await chrome.scripting.executeScript({target: {tabId: granted.tab_id},
        func: direction => history[direction](), args: [request.op]}); response.result = epoch;
    } else if (request.op === "load_status") {
      const tab = await chrome.tabs.get(granted.tab_id);
      const epoch = Number(p.epoch || 0);
      const failure = loadErrors.get(granted.tab_id);
      if (failure && Number(failure.epoch) >= epoch) {
        throw new Error(`Companion could not access the loaded page: ${failure.message}`);
      }
      response.result = {complete: tab.status === "complete" &&
        Number(completedLoads.get(granted.tab_id) || 0) >= epoch};
    } else if (request.op === "screenshot") {
      response.result = await chrome.tabs.captureVisibleTab(granted.window_id, {format: "png"});
    } else if (request.op === "tab_info") {
      response.result = await summary(await chrome.tabs.get(granted.tab_id));
    } else if (request.op === "evaluate") {
      if (!chrome.userScripts?.execute) {
        throw new Error("Evaluate JS requires Allow User Scripts for the Automadin Browser Companion in the browser extension details.");
      }
      const injected = await chrome.userScripts.execute({
        target: {tabId: granted.tab_id},
        world: "MAIN",
        injectImmediately: true,
        js: [{code: String(p.script || "")}],
      });
      const first = injected[0] || {};
      if (first.error) throw new Error(first.error);
      response.result = String(first.result ?? "");
    } else {
      response.result = await requestContent(
        granted.tab_id,
        {op: request.op, params: p},
      );
    }
  } catch (error) { response.error = String(error?.message || error); }
  post(response);
}
connect();
