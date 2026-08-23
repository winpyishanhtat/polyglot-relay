const $ = (selector) => document.querySelector(selector);
const toast = $("#toast");
let toastTimer;
let lastCaptionSignature = "";

// Populated once from /api/languages, then reused for every render pass.
let LANGS = [];
let LANG_BY_CODE = {};
let activeFilter = "All";

const EMPTY_COPY = {
  my: "ပထမစာတန်းကို စောင့်နေသည်…",
};
const DEFAULT_EMPTY_COPY = "Waiting for the first caption…";

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2400);
}

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function formatTime(timestamp) {
  return new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .format(new Date(timestamp * 1000));
}

function formatUptime(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds}s`;
}

function channelNo(index) {
  return String(index + 1).padStart(2, "0");
}

// ---- one-time setup, once language metadata is known ------------------

async function loadLanguages() {
  const response = await fetch("/api/languages", { cache: "no-store" });
  const data = await response.json();
  LANGS = data.languages;
  LANG_BY_CODE = Object.fromEntries(LANGS.map((lang) => [lang.code, lang]));
}

function buildChannelDial() {
  const dial = $("#channel-dial");
  dial.replaceChildren(...LANGS.map((lang, index) => {
    const pip = document.createElement("i");
    pip.className = "dial-pip";
    pip.dataset.code = lang.code;
    pip.dataset.group = lang.group;
    pip.style.height = `${14 + ((index * 7) % 20)}px`;
    pip.title = `${channelNo(index)} · ${lang.name}`;
    return pip;
  }));
}

function buildSourceSelect() {
  const select = $("#source-lang");
  const hint = $("#source-hint");
  for (const lang of LANGS) {
    const option = document.createElement("option");
    option.value = lang.code;
    option.textContent = `${lang.name} (${lang.native})`;
    select.append(option);
  }
  const updateHint = () => {
    hint.textContent = select.value === "auto"
      ? "Whisper identifies the spoken language for audio, Gemini for typed text - either way, the result carries through to skip translation for the matching channel"
      : "Matching caption card skips translation, and Whisper transcribes audio faster and more accurately when told the language up front - only pick this if you're sure, a wrong pick actively hurts transcription";
  };
  // Deliberately NOT remembered across page loads (was tried, then
  // reverted): a stale non-auto pick from a previous, different-language
  // upload silently forcing Whisper's language hint on unrelated new audio
  // produced confidently-wrong transcriptions - worse than just guessing
  // fresh each time. Always starts on "auto" so a forced hint only ever
  // happens when the user consciously chose it for *this* upload.
  updateHint();
  select.addEventListener("change", updateHint);
}

function buildGroupTabs() {
  const distinctGroups = new Set(LANGS.map((lang) => lang.group));
  const tabs = $("#group-tabs");
  // A single group filtering against itself is a no-op tab that only adds
  // confusion (e.g. an all-ASEAN language set showing "All channels" next
  // to a redundant "ASEAN" tab) - only show the filter bar when there's
  // more than one group to actually choose between.
  if (distinctGroups.size <= 1) {
    tabs.replaceChildren();
    tabs.hidden = true;
    activeFilter = "All";
    return;
  }
  tabs.hidden = false;
  const groups = ["All", ...distinctGroups];
  tabs.replaceChildren(...groups.map((group) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tab${group === activeFilter ? " active" : ""}`;
    button.textContent = group === "All" ? "All channels" : group;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(group === activeFilter));
    button.addEventListener("click", () => setFilter(group));
    return button;
  }));
}

function setFilter(group) {
  activeFilter = group;
  for (const tab of $("#group-tabs").children) {
    const isActive = tab.textContent === (group === "All" ? "All channels" : group);
    tab.classList.toggle("active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
  }
  for (const card of document.querySelectorAll(".caption-card")) {
    const show = activeFilter === "All" || card.dataset.group === activeFilter;
    card.style.display = show ? "" : "none";
  }
}

function buildCaptionGrid() {
  const grid = $("#caption-grid");
  grid.replaceChildren(...LANGS.map((lang, index) => {
    const card = document.createElement("article");
    card.className = "caption-card";
    card.dataset.lang = lang.code;
    card.dataset.group = lang.group;

    const header = document.createElement("header");
    const left = document.createElement("div");
    const codeBadge = document.createElement("span");
    codeBadge.className = "language-code";
    codeBadge.textContent = lang.code.toUpperCase();
    const titleWrap = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = `${channelNo(index)} · ${lang.name}`;
    const native = document.createElement("span");
    native.className = "native";
    native.textContent = lang.native;
    titleWrap.append(title, native);
    left.append(codeBadge, titleWrap);
    const liveTag = document.createElement("span");
    liveTag.className = "live-tag";
    liveTag.textContent = "Live";
    header.append(left, liveTag);

    const feed = document.createElement("div");
    feed.className = "caption-feed";
    feed.id = `captions-${lang.code}`;
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = EMPTY_COPY[lang.code] || DEFAULT_EMPTY_COPY;
    feed.append(empty);

    card.append(header, feed);
    return card;
  }));
}

// ---- per-tick rendering, driven by /api/status -------------------------

function makeNode(title, subtitle, online, opts = {}) {
  const node = document.createElement("div");
  node.className = `node${online ? " online" : ""}${opts.leader ? " leader" : ""}`;
  if (opts.group) node.dataset.group = opts.group;
  const top = document.createElement("div");
  top.className = "node-top";
  const name = document.createElement("strong");
  name.textContent = title;
  const dot = document.createElement("i");
  dot.className = "status-dot";
  top.append(name, dot);
  const detail = document.createElement("small");
  detail.textContent = subtitle;
  node.append(top, detail);
  if (opts.channelNo) {
    const badge = document.createElement("span");
    badge.className = "channel-no";
    badge.textContent = `CH ${opts.channelNo}`;
    node.prepend(badge);
  }
  return node;
}

function renderNetwork(data) {
  const brokers = $("#broker-nodes");
  brokers.replaceChildren(...data.brokers.map((broker) => makeNode(
    `Broker ${broker.id}`,
    broker.leader ? "Active leader" : broker.online ? "Standby replica" : "Offline",
    broker.online,
    { leader: broker.leader },
  )));

  const workers = $("#worker-nodes");
  workers.replaceChildren(...LANGS.map((lang, index) => {
    const worker = data.workers[lang.code] || { connected: false };
    return makeNode(
      lang.name,
      worker.connected ? worker.worker_id : "Reconnecting",
      worker.connected,
      { group: lang.group, channelNo: channelNo(index) },
    );
  }));
  $("#pool-count").textContent = LANGS.length;

  for (const pip of document.querySelectorAll(".dial-pip")) {
    const worker = data.workers[pip.dataset.code];
    pip.classList.toggle("live", Boolean(worker && worker.connected));
  }

  const healthy = data.brokers.some((broker) => broker.leader && broker.online)
    && Object.values(data.workers).every((worker) => worker.connected);
  const pill = $("#network-pill");
  pill.classList.toggle("online", healthy);
  pill.querySelector("span").textContent = healthy ? "Network healthy" : "Network recovering";
  $("#uptime").textContent = `Uptime ${formatUptime(data.uptime)}`;
  $("#queue-count").textContent = data.pending ? `${data.pending} queued` : "Queue clear";
}

function renderCaptions(captions, workers) {
  const signature = JSON.stringify(captions);
  if (signature === lastCaptionSignature) return;
  lastCaptionSignature = signature;
  for (const [lang, items] of Object.entries(captions)) {
    const card = document.querySelector(`.caption-card[data-lang="${lang}"]`);
    if (!card) continue;
    card.classList.toggle("connected", Boolean(workers[lang] && workers[lang].connected));
    const feed = $(`#captions-${lang}`);
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = EMPTY_COPY[lang] || DEFAULT_EMPTY_COPY;
      feed.replaceChildren(empty);
      continue;
    }
    const elements = items.slice(0, 12).map((item) => {
      const article = document.createElement("div");
      article.className = "caption-item";
      const text = document.createElement("p");
      text.textContent = item.text;
      article.append(text);
      // Skip when it'd just repeat the line above (passthrough/mock, or no
      // original was recorded for this chunk - e.g. a caption sent by the
      // CLI producer instead of the dashboard's own submit flow).
      if (item.original_text && item.original_text !== item.text) {
        const original = document.createElement("p");
        original.className = "caption-original";
        const sourceLabel = item.original_source_lang && item.original_source_lang !== "auto"
          ? (LANG_BY_CODE[item.original_source_lang]?.name || item.original_source_lang)
          : "auto-detected";
        original.textContent = `“${item.original_text}” — ${sourceLabel}`;
        article.append(original);
      }
      const meta = document.createElement("div");
      meta.className = "caption-meta";
      const time = document.createElement("span");
      time.textContent = formatTime(item.timestamp);
      const worker = document.createElement("span");
      worker.className = `backend-tag backend-${item.translation_backend || "relay"}`;
      worker.textContent = item.translation_backend === "passthrough" ? "same language" : (item.translation_backend || item.worker_id || "relay");
      meta.append(time, worker);
      article.append(meta);
      return article;
    });
    feed.replaceChildren(...elements);
  }
}

function renderActivity(items) {
  const list = $("#activity-list");
  if (!items.length) return;
  const elements = items.slice(0, 12).map((item) => {
    const row = document.createElement("div");
    row.className = `activity-item ${item.kind}`;
    const dot = document.createElement("i");
    const message = document.createElement("span");
    message.textContent = item.message;
    const time = document.createElement("time");
    time.textContent = formatTime(item.timestamp);
    row.append(dot, message, time);
    return row;
  });
  list.replaceChildren(...elements);
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard API unavailable");
    const data = await response.json();
    renderNetwork(data);
    renderCaptions(data.captions, data.workers);
    renderActivity(data.activity);
  } catch (error) {
    const pill = $("#network-pill");
    pill.classList.remove("online");
    pill.querySelector("span").textContent = "Dashboard offline";
  }
}

// ---- audio/video input: file upload + live browser recording -----------
// Captions now only ever enter via an uploaded file or a recording -
// transcription happens server-side (Whisper, via web_ui.py's
// /api/captions/audio), so all this sends is the raw media blob.

function setComposerBusy(busy, statusText) {
  $("#record-button").disabled = busy;
  $("#audio-file-input").disabled = busy;
  document.querySelector(".file-button").classList.toggle("is-disabled", busy);
  $("#composer-status").textContent = statusText || "Upload a file or record to send a caption";
}

async function postAudio(blob, sourceLang, label) {
  setComposerBusy(true, `Transcribing ${label}…`);
  try {
    const response = await fetch(`/api/captions/audio?source_lang=${encodeURIComponent(sourceLang)}`, {
      method: "POST",
      headers: { "Content-Type": blob.type || "application/octet-stream" },
      body: blob,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Upload failed");
    const count = data.captions || 1;
    const detectedLang = data.detected_language && LANG_BY_CODE[data.detected_language];
    const detectedNote = detectedLang ? ` — detected ${detectedLang.name}` : "";
    showToast(`Sent ${count} caption${count === 1 ? "" : "s"} to the relay${detectedNote}`);
    refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    setComposerBusy(false);
  }
}

async function handleFileUpload(event) {
  const file = event.target.files[0];
  event.target.value = ""; // allow re-selecting the same file next time
  if (!file) return;
  await postAudio(file, $("#source-lang").value, "upload");
}

function pickRecorderMimeType(includeVideo) {
  const candidates = includeVideo
    ? ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm", "video/mp4"]
    : ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  return candidates.find((type) => window.MediaRecorder && MediaRecorder.isTypeSupported(type)) || "";
}

let mediaRecorder = null;
let recordedChunks = [];
let recordStream = null;
let recordTimerInterval = null;
let recordStartedAt = 0;

function updateRecordTimer() {
  const elapsed = Math.floor((Date.now() - recordStartedAt) / 1000);
  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const ss = String(elapsed % 60).padStart(2, "0");
  $("#record-timer").textContent = `${mm}:${ss}`;
}

async function startRecording() {
  const includeVideo = $("#include-video").checked;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: includeVideo });
  } catch (error) {
    showToast("Microphone/camera permission was denied");
    return;
  }
  const mimeType = pickRecorderMimeType(includeVideo);
  recordStream = stream;
  recordedChunks = [];
  mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  mediaRecorder.addEventListener("dataavailable", (event) => {
    if (event.data.size) recordedChunks.push(event.data);
  });
  mediaRecorder.addEventListener("stop", async () => {
    const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || "application/octet-stream" });
    recordedChunks = [];
    await postAudio(blob, $("#source-lang").value, "recording");
  });
  mediaRecorder.start();

  const preview = $("#record-preview");
  if (includeVideo) {
    preview.srcObject = stream;
    preview.hidden = false;
  }

  recordStartedAt = Date.now();
  $("#record-timer").hidden = false;
  updateRecordTimer();
  recordTimerInterval = setInterval(updateRecordTimer, 250);
  $("#record-label").textContent = "Stop";
  $("#record-button").classList.add("recording");
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  if (recordStream) {
    for (const track of recordStream.getTracks()) track.stop();
    recordStream = null;
  }
  clearInterval(recordTimerInterval);
  $("#record-timer").hidden = true;
  $("#record-preview").hidden = true;
  $("#record-preview").srcObject = null;
  $("#record-label").textContent = "Record";
  $("#record-button").classList.remove("recording");
}

$("#audio-file-input").addEventListener("change", handleFileUpload);
$("#record-button").addEventListener("click", () => {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    stopRecording();
  } else {
    startRecording();
  }
});

$("#demo-button").addEventListener("click", async () => {
  try {
    await post("/api/demo");
    showToast("Four-caption demo started");
  } catch (error) { showToast(error.message); }
});

$("#clear-button").addEventListener("click", async () => {
  await post("/api/clear");
  lastCaptionSignature = "";
  showToast("Caption history cleared");
  refresh();
});

setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString(); }, 1000);

async function boot() {
  await loadLanguages();
  buildChannelDial();
  buildSourceSelect();
  buildGroupTabs();
  buildCaptionGrid();
  setInterval(refresh, 1000);
  refresh();
}

boot();
