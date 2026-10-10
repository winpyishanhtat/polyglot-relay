const $ = (selector) => document.querySelector(selector);
const toast = $("#toast");
let toastTimer;
let lastCaptionSignature = "";

// Populated once from /api/languages, then reused for every render pass.
let LANGS = [];
let LANG_BY_CODE = {};
let activeFilter = "All";
let uploadSending = false;
let recordedSource = "en";
const STATE_LABELS = {source: "Source", awaiting_broker: "Sending", queued: "Queued", processing: "Processing",
  quota_wait: "Quota wait", retrying: "Retrying", translating: "Translating", completed: "Done",
  failed: "Failed", unavailable: "No worker", interrupted: "Interrupted"};

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
    hint.textContent = `${LANG_BY_CODE[select.value]?.name || "Selected language"} is captioned as-is in its own channel; the other 8 channels translate each chunk - all 9 channels update together.`;
    for (const card of document.querySelectorAll(".caption-card")) {
      card.classList.toggle("source-channel", card.dataset.lang === select.value);
    }
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
    liveTag.textContent = "Waiting";
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
  const leader = data.brokers.find(b => b.online && b.leader);
  const brokers = $("#broker-nodes");
  brokers.replaceChildren(...data.brokers.map((broker) => makeNode(
    `Broker ${broker.id}`,
    broker.leader ? `Active leader · ${(broker.members || []).length} workers` : broker.online ? "Standby · monitoring heartbeats" : "Offline / unreachable",
    broker.online,
    { leader: broker.leader },
  )));

  const workers = $("#worker-nodes");
  workers.replaceChildren(...LANGS.map((lang, index) => {
    const worker = data.workers[lang.code] || { connected: false };
    const members = (leader?.members || []).filter(m => m.lang === lang.code);
    const jobs = (leader?.jobs || []).filter(j => j.lang === lang.code);
    const running = jobs.filter(j => ["processing", "translating", "quota_wait", "retrying"].includes(j.status));
    const queued = jobs.filter(j => j.status === "queued").length;
    const failed = jobs.filter(j => ["failed", "unavailable"].includes(j.status)).length;
    const node = makeNode(
      lang.name,
      `${members.length} worker${members.length === 1 ? "" : "s"} · ${queued} queued · ${running.length} active${failed ? ` · ${failed} failed` : ""}`,
      members.length > 0,
      { group: lang.group, channelNo: channelNo(index) },
    );
    for (const member of members) {
      const detail = document.createElement("small");
      const job = running.find(j => j.worker_id === member.worker_id);
      detail.textContent = `${member.worker_id} · ${job ? STATE_LABELS[job.status] : "Ready"} · ♥ ${member.heartbeat_age}s`;
      node.append(detail);
    }
    node.classList.toggle("working", running.length > 0);
    const card = document.querySelector(`.caption-card[data-lang="${lang.code}"]`);
    if (card) {
      card.classList.toggle("connected", worker.connected);
      card.classList.toggle("source-channel", lang.code === $("#source-lang").value);
      card.querySelector(".live-tag").textContent = running.length ? STATE_LABELS[running[0].status]
        : lang.code === $("#source-lang").value ? "Source" : worker.connected ? "Ready" : "Offline";
    }
    return node;
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

function renderChunks(data) {
  const chunks = data.chunks || [];
  const targets = chunks.flatMap(c => Object.values(c.targets));
  const done = targets.filter(t => t.status === "completed" || t.status === "source").length;
  const failed = targets.filter(t => ["failed", "unavailable", "interrupted"].includes(t.status)).length;
  $("#translation-progress").textContent = `${chunks.length} chunk${chunks.length === 1 ? "" : "s"} · ${done}/${targets.length} translations done${failed ? ` · ${failed} need attention` : ""}`;
  const media = data.media || {status: "idle"};
  const live = data.live || {};
  $("#media-progress").textContent = liveActive ? `Live: ${live.slices || 0} slice${live.slices === 1 ? "" : "s"} transcribed · ${live.chunks || 0} caption${live.chunks === 1 ? "" : "s"} sent${live.dropped ? ` · ${live.dropped} skipped (transcriber behind)` : ""}`
    : media.status === "transcribing" ? `Transcribing file… ${media.chunks} speech chunks sent so far. Earlier chunks are already in the pools.`
    : media.status === "failed" ? `Transcription failed: ${media.error}`
    : media.status === "transcribed" ? `Transcription finished: ${media.chunks} speech chunks. Watch the rows below for translation progress.`
    : "Upload a file, record then stop, or submit text. Each row follows one chunk across every channel.";
  if (!uploadSending && !liveActive && (!mediaRecorder || mediaRecorder.state !== "recording")) {
    setComposerBusy(media.status === "transcribing", media.status === "transcribing" ? "Transcribing; follow chunk progress below" : undefined);
  }
  const head = document.createElement("tr");
  for (const title of ["Chunk / source", ...LANGS.map(l => l.code.toUpperCase())]) {
    const th = document.createElement("th"); th.textContent = title; th.scope = "col"; head.append(th);
  }
  $("#chunk-table thead").replaceChildren(head);
  $("#chunk-table tbody").replaceChildren(...chunks.slice(-40).map(chunk => {
    const row = document.createElement("tr");
    const title = document.createElement("th"); title.scope = "row";
    const label = document.createElement("strong"); label.textContent = `#${chunk.number} · ${LANG_BY_CODE[chunk.source_lang]?.name || "Auto"}`;
    const text = document.createElement("small"); text.textContent = chunk.text; title.append(label, text); row.append(title);
    for (const lang of LANGS) {
      const cell = document.createElement("td"); const state = chunk.targets[lang.code] || {status: "queued"};
      const badge = document.createElement("span"); badge.className = `chunk-state state-${state.status}`;
      badge.textContent = STATE_LABELS[state.status] || state.status;
      badge.title = [state.worker_id, state.detail].filter(Boolean).join(" · ");
      cell.append(badge); row.append(cell);
    }
    return row;
  }));
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
    const elements = [...items].sort((a, b) => (a.number || 0) - (b.number || 0)).slice(-12).map((item) => {
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
      time.textContent = `${item.number ? `#${item.number} · ` : ""}${formatTime(item.timestamp)}`;
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
    renderChunks(data);
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
  $("#source-lang").disabled = busy;
  $("#include-video").disabled = busy || $("#live-mode").checked;
  $("#live-mode").disabled = busy;
  $("#demo-button").disabled = busy;
  document.querySelector(".file-button").classList.toggle("is-disabled", busy);
  $("#composer-status").textContent = statusText || "Upload a file or record to send a caption";
}

async function postAudio(blob, sourceLang, label) {
  if (blob.size > 25 * 1024 * 1024) { showToast("File exceeds 25 MB. Please use a shorter clip."); setComposerBusy(false); return; }
  uploadSending = true;
  setComposerBusy(true, `Transcribing ${label}…`);
  try {
    const response = await fetch(`/api/captions/audio?source_lang=${encodeURIComponent(sourceLang)}`, {
      method: "POST",
      headers: { "Content-Type": blob.type || "application/octet-stream" },
      body: blob,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Upload failed");
    showToast("Upload accepted. Follow each chunk in Chunk journey.");
    refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    uploadSending = false;
    setComposerBusy(false);
    refresh();
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
  if ($("#live-mode").checked) return startLive();
  recordedSource = $("#source-lang").value;
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
    await postAudio(blob, recordedSource, "recording");
  });
  mediaRecorder.start();
  $("#source-lang").disabled = true;
  $("#audio-file-input").disabled = true;
  $("#include-video").disabled = true;
  $("#demo-button").disabled = true;
  $("#composer-status").textContent = "Recording… press Stop to transcribe and translate in chunks";

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

// ---- live mode: captions while the speaker is still talking ---------------
// Instead of one blob at Stop, the microphone is cut into short slices that
// are each a complete, independently decodable file (a MediaRecorder only
// writes its container header once, so cutting one recorder's output would
// leave every slice after the first undecodable). A new recorder is started
// before the previous one is stopped so no audio falls between slices.
let liveActive = false;
let liveTimer = null;
let liveRecorder = null;
let liveStream = null;
let liveSource = "en";
let liveMimeType = "";
let liveSendChain = Promise.resolve();
const LIVE_MIN_SLICE_MS = 700; // a shorter tail (e.g. at Stop) is mostly noise

function startLiveSlice() {
  const parts = [];
  const startedAt = Date.now();
  const recorder = liveMimeType ? new MediaRecorder(liveStream, { mimeType: liveMimeType }) : new MediaRecorder(liveStream);
  recorder.addEventListener("dataavailable", (event) => { if (event.data.size) parts.push(event.data); });
  recorder.addEventListener("stop", () => {
    if (Date.now() - startedAt < LIVE_MIN_SLICE_MS) return;
    const blob = new Blob(parts, { type: recorder.mimeType || "application/octet-stream" });
    if (blob.size) sendLiveSlice(blob, liveSource);
  });
  recorder.start();
  return recorder;
}

function rotateLiveSlice() {
  const previous = liveRecorder;
  liveRecorder = startLiveSlice();
  if (previous && previous.state !== "inactive") previous.stop();
}

// Posts are chained so slices reach the server in the order they were
// recorded; the server answers immediately, so the chain never stalls.
function sendLiveSlice(blob, sourceLang) {
  liveSendChain = liveSendChain.then(async () => {
    try {
      const response = await fetch(`/api/live/slice?source_lang=${encodeURIComponent(sourceLang)}`, {
        method: "POST",
        headers: { "Content-Type": blob.type || "application/octet-stream" },
        body: blob,
      });
      if (response.status === 429) showToast("Live transcription is behind - a slice was skipped");
      else if (!response.ok) showToast((await response.json().catch(() => ({}))).error || "Live slice failed");
    } catch (error) {
      showToast("Lost connection to the dashboard");
    }
  });
}

async function startLive() {
  const sourceLang = $("#source-lang").value;
  if (!LANGS.some((lang) => lang.code === sourceLang)) {
    showToast("Select the spoken language before going live");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (error) {
    showToast("Microphone permission was denied");
    return;
  }
  let sliceSeconds = 4;
  try {
    const status = await (await fetch("/api/status", { cache: "no-store" })).json();
    sliceSeconds = status.settings.live_slice_seconds || sliceSeconds;
  } catch (error) { /* keep the default */ }

  liveActive = true;
  liveStream = stream;
  liveSource = sourceLang;
  liveMimeType = pickRecorderMimeType(false);
  liveRecorder = startLiveSlice();
  liveTimer = setInterval(rotateLiveSlice, sliceSeconds * 1000);

  for (const id of ["#source-lang", "#audio-file-input", "#include-video", "#live-mode", "#demo-button"]) $(id).disabled = true;
  $("#composer-status").textContent = `Live - speak now; captions appear every ~${sliceSeconds}s`;
  recordStartedAt = Date.now();
  $("#record-timer").hidden = false;
  updateRecordTimer();
  recordTimerInterval = setInterval(updateRecordTimer, 250);
  $("#record-label").textContent = "Stop";
  $("#record-button").classList.add("recording");
}

function stopLive() {
  liveActive = false;
  clearInterval(liveTimer);
  if (liveRecorder && liveRecorder.state !== "inactive") liveRecorder.stop(); // flushes the last slice
  liveRecorder = null;
  if (liveStream) {
    for (const track of liveStream.getTracks()) track.stop();
    liveStream = null;
  }
  clearInterval(recordTimerInterval);
  $("#record-timer").hidden = true;
  $("#record-label").textContent = "Record";
  $("#record-button").classList.remove("recording");
  setComposerBusy(false);
}

$("#live-mode").addEventListener("change", (event) => {
  $("#include-video").disabled = event.target.checked;
  if (event.target.checked) $("#include-video").checked = false;
});

$("#audio-file-input").addEventListener("change", handleFileUpload);
$("#record-button").addEventListener("click", () => {
  if (liveActive) {
    stopLive();
  } else if (mediaRecorder && mediaRecorder.state === "recording") {
    stopRecording();
  } else {
    startRecording();
  }
});

$("#demo-button").addEventListener("click", async () => {
  try {
    $("#source-lang").value = "en";
    $("#source-lang").dispatchEvent(new Event("change"));
    await post("/api/demo");
    showToast("Four-caption demo started");
  } catch (error) { showToast(error.message); }
});

$("#text-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#text-form button"); button.disabled = true;
  try {
    await post("/api/captions", {text: $("#caption-text").value, source_lang: $("#source-lang").value});
    $("#caption-text").value = "";
    showToast("Chunk queued for all 9 language pools");
    refresh();
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
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
  async function poll() { await refresh(); setTimeout(poll, 1000); }
  poll();
}

boot();
