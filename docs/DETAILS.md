# Polyglot Relay — Technical Deep Dive

The full rationale, tradeoffs, and troubleshooting notes behind everything in the [main README](../README.md). Sections here map directly onto a distributed-systems coursework report — pull whatever's relevant straight into it.

---

## Why this project

Live multilingual communication (classrooms, community meetings, disaster briefings) usually assumes everyone shares a language or relies on a single human interpreter — a single point of failure and a bottleneck. This project explores a distributed alternative where translation capacity per language can scale independently, and no single component's failure takes down the whole system — including the coordination layer itself.

## Scope

**In scope:**
- Live microphone capture from a speaker
- Streaming speech-to-text via self-hosted Whisper
- A **replicated broker** (2 instances) that elects a leader and fails over automatically if the leader dies
- **Dynamic worker registration** — translator workers register themselves with the current leader broker at startup; nothing is hardcoded
- **Load-balanced worker pools** — each language can have 1+ worker instances; the broker distributes transcript chunks round-robin across a language's registered instances
- Parallel translation into 9 ASEAN-focused languages: English (ASEAN's official working language, covers Singapore) plus one representative language per remaining ASEAN member state (see `common/config.py` for the full list)
- Live caption display per listener/language
- Fault isolation and recovery at two levels: worker-level (a dead translator doesn't affect other languages or other instances of the same language) and broker-level (a dead leader doesn't stop the whole pipeline)
- Basic accuracy evaluation (WER for transcription, BLEU for translation) against a small benchmark set

**Explicitly out of scope (stretch goals):**
- Text-to-speech output (captions only)
- Multiple simultaneous speakers
- Persistent storage / session history across restarts
- Authentication
- Full Raft-style log replication for broker state (we use a simpler leader-election + state-resync approach — see "Design tradeoffs" below)

## Component responsibilities

- **STT service**: the command-line way captions enter the system. Takes either a WAV file (`--input-file`) or a live microphone stream (default, no flags), buffers audio into short chunks, runs each chunk through a local Whisper model, and sends the transcript to whichever broker is currently the leader (with retry/reconnect logic if the leader changes mid-session). Raw PCM audio is only attached if you pass `--send-audio`; by default only text goes out, since the plain-text translation path is faster and more reliable across many languages than opening a live audio session per chunk (see "Translation modes" below). The web dashboard offers the same audio-in experience from a browser instead — upload an MP3/audio/video file, or record live through the mic (optionally with camera) — transcribed server-side via the same Whisper pipeline; there is no typed-text input anymore.
- **Broker replicas (2)**: only the current **leader** accepts connections from the STT service and translator workers. The **standby** replica exchanges heartbeats with the leader; if it misses enough heartbeats, it triggers a leader election (see below) and promotes itself. Workers and the STT service detect the leader change via failed connection + a shared list of known broker addresses, and reconnect to whichever one is now active.
- **Leader election**: a simplified **Bully algorithm** — each broker replica has a fixed, known ID. On startup or on detecting the leader is unreachable, a replica broadcasts an election message to replicas with higher IDs; if none respond, it becomes leader and announces itself. With only 2 replicas this is simple to reason about and correct to implement, while still being a genuine leader election protocol, not just a hardcoded primary/backup.
- **Dynamic worker registration**: translator workers do **not** appear in any static config. On startup, a worker connects to the current leader broker and sends a `register` message (`lang`, `worker_id`, `host`, `port`). The broker adds it to that language's pool. This means adding capacity for a language is "start another worker process," not "edit a config and restart everything." Adding a *new* language is one line in `common/config.py`'s `LANGUAGES` dict — every other module (broker, workers, dashboard, UI) derives its language list from that single source.
- **Worker pools + load balancing**: the broker keeps a list of registered worker instances per language and distributes incoming transcript chunks **round-robin** across a language's pool. This is real horizontal scaling — you can demonstrate throughput increasing as you add a second English worker under load, distinct from fault isolation (which is about correctness under failure, not capacity under load). Any language can be scaled the same way; English ships with 2 replicas by default to make the demo obvious.
- **Listener clients**: connect to their language's currently-assigned worker instance (discovered via the broker) and render captions live. The web dashboard listens to all 9 pools at once.

## Translation modes, in depth

Every worker calls out to a translation backend through `translator_worker.py`'s `Translator` class, which supports a pluggable set of them, plus a zero-cost passthrough. `TRANSLATION_BACKEND` (default `auto`) controls which are tried:

| Mode | When it's used | Trade-off |
|---|---|---|
| **Passthrough** (`passthrough`) | Automatic whenever the caption's declared source language matches a worker's own language. | Instant, no API call at all — translating a language into itself would be a no-op anyway. |
| **LibreTranslate** (`libretranslate`) | `auto` (default) or `libretranslate`. Self-hosted, open-source engine — see `compose.libretranslate.yaml`. Tried first if the worker's language is loaded (discovered at runtime, not hardcoded). | Free forever, no quota, no account — confirmed to cover 6 of the 9 languages; Burmese/Khmer/Lao are not in Argos Translate's catalog and always need the Gemini fallback. |
| **Text** (`gemini-text`, model `gemini-pro-latest` on the `paid` tier, `gemini-3.6-flash` on `free`) | `auto` (fallback) or `gemini`. One REST call per caption, using the transcript Whisper already produced. | Fast, accurate, works for all 9 languages — but metered by Google's API quota (see "Rate limits" below). The system instruction frames this explicitly as live spoken-conversation captioning, not document translation: it asks for natural/conversational phrasing over stiff word-for-word translation, and tells the model the fragment may be an incomplete sentence rather than something to complete or restructure. |
| **Live** (`gemini-live`, model `gemini-3.5-live-translate-preview`) | Opt-in via `stt_service.py --send-audio`. Re-transcribes and translates directly from raw audio over a duplex session. | Slower and more failure-prone (one live session per chunk, per language, per worker) — not all 9 target languages are guaranteed to be supported by Gemini Live yet. Useful for demonstrating speech-to-speech translation specifically, not for everyday routing. |

If translation ever silently stops working (workers show "connected" on the dashboard but no captions arrive), check the worker's logs first — a failed live-audio session is the most common cause, and switching back to the default text-only mode is the fix.

### Source language ("any language to multiple languages")

Every caption can declare which language it's spoken in — the dashboard's composer has a "Speaking in" dropdown (defaults to **Auto-detect**), and `stt_service.py` exposes the same choice via `--source-lang`. This is not just metadata: a worker whose own language matches the declared source skips translation entirely (passthrough, above) instead of assuming every caption starts in English, which is what earlier versions of this project did implicitly. Pick "Auto-detect" and Gemini identifies the source language itself for every target — nothing is hardcoded to English anywhere in the translation path. A concrete pick (not "Auto-detect") is also passed to Whisper itself for audio uploads/recordings (`web_ui.py`'s `submit_audio` → `stt_service.py`'s `transcribe_file`, `language=`) — skipping its own detection pass is both faster and more accurate than guessing, but only helps when the pick is actually correct for that clip; a stale pick left over from a previous, different-language upload will actively hurt accuracy, which is why the dashboard resets to Auto-detect on every page load rather than remembering your last choice.

Every caption card in the dashboard also shows the **original pre-translation text** in a small quoted line above the translation (skipped when it'd just repeat the same text, e.g. passthrough) — `DashboardState.record_original` keeps a bounded (200-entry) map of `chunk_id → original text` alongside the per-language caption history, so you can always see exactly what was actually said, not just the translated result.

### Audio processing (`stt_service.py`'s `transcribe_file`)

An uploaded/recorded file gets transcribed in **one whole-file Whisper pass**, not pre-sliced into fixed-size windows — slicing first (the original approach) cuts sentences mid-word at arbitrary boundaries, which both wastes compute (each slice pays a full padded-to-30s encoder pass regardless of its real length) and hurts accuracy (a truncated fragment often transcribes as empty text, silently dropping that part of the audio). Whisper's own segmentation handles natural sentence/pause boundaries far better than blind fixed-window slicing. Live microphone capture (`transcribe_microphone`) is a deliberate exception — real-time streaming genuinely needs small, bounded-latency chunks, which a finished recording does not.

Two independent signals guard against Whisper **hallucination** (fluently transcribing noise, music, or silence as if it were real speech) beyond just "does this look empty":
1. **Whisper's own confidence**: a segment with high `no_speech_prob` (thinks it isn't speech at all) *and* low `avg_logprob` (wasn't confident in the words either) is dropped — the same thresholds Whisper's own CLI uses.
2. **A second, independent opinion**: `py3langid` (a small, self-contained text-language classifier — no heavy model, no network call) checks whether the transcribed *text* is confidently identifiable as the expected language. A segment that disagrees with high confidence gets dropped too — this catches a different failure mode: text Whisper transcribed fluently and confidently, but in the wrong language entirely.

Whisper's own per-file language detection (when "Auto-detect" is left selected) is propagated forward as the caption's `source_lang`, not left as the literal string `"auto"` — this unlocks the passthrough shortcut for the matching language and the known-source Gemini prompt variant for every other worker, instead of every worker guessing independently. The dashboard also surfaces this back to you: a successful "Auto-detect" upload shows a toast like *"Sent 2 captions to the relay — detected Thai"*.

### Rate limits and retries (why only 3-4 languages used to translate on a free key)

A single caption is broadcast to all 9 language pools by the broker at essentially the same instant (`broker.route_chunk`) — though in practice only Burmese/Khmer/Lao actually call Gemini once LibreTranslate is running, so this mostly matters if you're forcing `TRANSLATION_BACKEND=gemini`. Google's Gemini free tier enforces a *hard* 5-requests-per-minute ceiling **per project+model, shared across every worker** — not a soft "usually fine" limit, and not per-worker — so firing 9 concurrent API calls at once will always trip it on a free key; only whichever 5 land first succeed, and the rest fail (visible as `429 RESOURCE_EXHAUSTED` in a worker's logs) unless the mitigations below cover the gap. A billing-enabled paid-tier key (`GEMINI_TIER=paid`, the default) raises that ceiling enough that none of this is a concern:

1. **Shared rate gate** (`gemini_rate_gate.py`, `GEMINI_TIER=free` only): every Gemini-backed worker blocks on `GET /acquire` against a small shared HTTP service (`compose.real.yaml`'s `gemini-rate-gate`) before calling Gemini, which enforces the real 5/minute ceiling across *all* workers together via a sliding window — not just spreads out each worker's own guess at when to fire. This is what actually keeps a 9-language free-tier broadcast from mostly 429ing; expect later languages in a burst to visibly lag behind earlier ones (up to the low tens of seconds) rather than error out. Unreachable gate → a logged warning, not a failure; translation falls back to retry-with-backoff alone.
2. **Retry with backoff**: any retryable error (`429`, `503`, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`) is retried up to `GEMINI_MAX_RETRIES` times (default 5) with exponential backoff and jitter, so a transient rate-limit rejection recovers on its own a few seconds later instead of being a permanent failure.
3. **Per-worker stagger**: on `GEMINI_TIER=free`, each worker also sleeps a small, deterministic offset based on its position in `TARGET_LANGUAGES` (about 0.08s more per language, ~0.6s total spread across all 9) before its first attempt on a chunk, ahead of the rate gate above. On the `paid` tier this defaults to `0s`, since the higher RPM ceiling doesn't need it.
4. **Passthrough** also shrinks every burst by one call whenever the caption's source language is known.
5. **Daily quota is a separate, harder cap**: a `429` whose `quotaId` mentions `PerDay` is not retried at all (see `_is_daily_quota_exhausted`), because it will not clear for hours regardless of backoff — this applies on both tiers, just at very different daily ceilings. See "Gemini API tier" below if this keeps happening on a key that's genuinely billing-enabled — it may be a tier-propagation gap on a specific model rather than a real quota problem.

The recommended LibreTranslate-hybrid setup (`compose.libretranslate.yaml`) doesn't wire up `gemini-rate-gate` at all — only Burmese/Khmer/Lao ever reach Gemini there (3 concurrent calls, safely under the free tier's 5/minute ceiling on their own), so retry-with-backoff alone is enough. The gate only matters if you're running `compose.real.yaml` (pure Gemini, all 9 languages) on a free-tier key.

If every single language consistently fails even with the rate gate running, check the worker logs for a non-retryable error (`401`/`403`/invalid key) instead — that's a key problem, not a quota one.

### Gemini API tier

`GEMINI_TIER` (default `paid`) tells every worker what kind of `GEMINI_API_KEY` it's holding, and picks defaults accordingly — an explicit `GEMINI_TEXT_MODEL` or `POLYGLOT_STAGGER_STEP_SECONDS` always wins over either tier's default:

| Tier | `GEMINI_TEXT_MODEL` default | Per-worker stagger | When to use it |
|---|---|---|---|
| `paid` (default) | `gemini-pro-latest` — higher translation quality | none (`0s`) | A Google AI Studio / Cloud key with billing enabled. Paid-tier requests-per-minute quota comfortably absorbs all 9 workers calling Gemini at the same instant, so the free-tier stagger workaround is unnecessary, and the higher-quality Pro model is affordable to run by default. |
| `free` | `gemini-3.6-flash` — cheaper, faster | ~0.08s × language position (~0.6s spread across all 9) | A free Google AI Studio key with no billing account attached. Keeps the original rate-limit-safe behavior described above. |

Set it once before starting your workers:

```bash
export GEMINI_TIER=free   # only if your key is NOT billing-enabled
```

(On Windows PowerShell: `$env:GEMINI_TIER = "free"`.) Leave it unset to use the `paid` default.

**If a billing-enabled key still gets `429`s tagged `-FreeTier`** (visible in a worker's logs, and in Google's own error payload's `quotaId`) despite Google Cloud Console and `ai.dev/rate-limit` both showing paid-tier limits for the project: this is a real, observed inconsistency between what Google's billing/tier system shows as active and what a *specific model* actually enforces server-side — the tier upgrade can propagate to some models before others. Confirmed on a live key where `gemini-3.6-flash` stayed stuck on free-tier enforcement (`limit: 20`/day, `limit: 5`/min) while `gemini-3.7-flash` correctly reflected the paid tier with no such errors. If you hit this, try pinning `GEMINI_TEXT_MODEL` to a different (typically newer) model name than the tier default before assuming the account itself is broken — it may simply be that specific model version that hasn't caught up yet. Note also that switching models resets the daily-quota counter (each model tracks its own), so this buys a fresh batch of requests, not a permanent fix if the underlying propagation gap affects your whole account.

## Distributed programming concepts demonstrated (map this section directly into your report)

| Concept | Where it shows up |
|---|---|
| Leader election | Bully algorithm between the two broker replicas |
| Replication / failover | Standby broker takes over if the leader dies, with workers and the STT service reconnecting transparently |
| Dynamic service registration | Workers register at runtime; no static worker list |
| Load balancing / sharding | Round-robin chunk distribution across a language's worker pool (9 independent pools, English scaled to 2 instances) |
| Failure detection | Heartbeats at two levels — broker-to-broker (for leader liveness) and worker-to-broker (for pool health) |
| Fault isolation | A dead worker only affects its own language; a dead non-leader broker doesn't affect the system at all |

## Design tradeoffs (be upfront about these in your report)

- **Bully algorithm over Raft**: Raft is the more "complete" consensus algorithm (handles log replication and multiple failure scenarios more rigorously), but is significantly more implementation effort for a 2-replica broker. Bully election gives you a real, correct leader-election mechanism without that overhead — a reasonable engineering tradeoff for this scope, and worth explicitly naming as a design decision rather than an oversight.
- **No persistent broker state**: if both broker replicas die simultaneously, in-flight registration state is lost and workers must re-register on reconnect. This is an accepted limitation for MVP scope — a production system would persist registration state, not just hold it in memory.
- **Round-robin, not load-aware, balancing**: chunks are split evenly by count, not by each worker's actual current load. A more sophisticated version would track per-worker latency and route accordingly — noted as a stretch goal.

## Stretch goals (if time allows)

- Load-aware (not just round-robin) balancing across worker pools
- Text-to-speech output per language
- Web-based listener UI instead of terminal
- A third broker replica to demonstrate election with more than 2 candidates

## Running natively (no Docker)

```bash
# Terminals 1-2 — start both broker replicas (order doesn't matter; they elect a leader themselves)
python broker.py --id 1
python broker.py --id 2

# Terminal 3 — start the STT service, one of:
python stt_service.py                          # live microphone (default)
python stt_service.py --input-file speech.wav   # a WAV file instead

# Terminals 4-6+ — start translator workers, any number per language
python translator_worker.py --lang en
python translator_worker.py --lang en   # second EN instance — same pool, load balanced
python translator_worker.py --lang th
python translator_worker.py --lang my

# Terminals — start a listener client per language
python listener_client.py --lang en
python listener_client.py --lang th
python listener_client.py --lang my
```

Speak into your microphone (or point `--input-file` at a WAV) — captions should appear per language within a few seconds. The `--source-lang` flag lets you declare which ASEAN language the speaker is using instead of relying on auto-detection (e.g. `--source-lang th` for a Thai speaker) — see "Source language" above.

**Demo script for the fault-tolerance story:**
1. Kill the leader broker process (`Ctrl+C`). Show the standby detect it within ~6-8 seconds, run the election, and become the new leader — workers and STT service reconnect automatically, pipeline keeps running.
2. Kill one of the two English translator instances mid-session. Show captions keep flowing for English (the remaining instance picks up all the load) and every other language pool is completely unaffected.
3. Start a fresh second English worker mid-session and show it gets registered and starts receiving a share of the load without restarting anything else.

### Local smoke test (no cloud account or microphone)

The implementation includes deterministic mock translation and stdin transcript input so the distributed behavior can be demonstrated before installing the optional ML/cloud dependencies:

```bash
# separate terminals
python broker.py --id 1
python broker.py --id 2
python translator_worker.py --lang en --mock
python translator_worker.py --lang th --mock
python translator_worker.py --lang my --mock
python listener_client.py --lang en
python stt_service.py --stdin
```

Type one caption per line in the STT terminal. For real audio and translation, install `requirements.txt`, configure `GEMINI_API_KEY`, and run workers without `--mock`.

The selected worker sends its completed translation back through the leader, which relays it to every worker in that language pool. This keeps translation load-balanced while ensuring a listener connected to any pool member receives the complete caption stream.

### Gemini Live translation mode

Real workers use the current `google-genai` SDK. Audio captured from a WAV file or microphone is carried alongside the Whisper transcript as 16 kHz PCM and is translated through `gemini-3.5-live-translate-preview`. The worker streams the audio in 100 ms frames and publishes Gemini's output transcription as the live caption. Text submitted through the dashboard or `--stdin` cannot be sent to Live Translate because that model accepts audio only, so it is translated with the text model instead. Both model names can be overridden directly with `GEMINI_LIVE_MODEL` and `GEMINI_TEXT_MODEL`, which always win over the tier default.

The round-robin design treats each 2.5-second audio chunk as an independent Live API session. This preserves horizontal worker balancing; a continuous speech-to-speech session would instead need sticky routing to one worker.

## Evaluation

Run `python eval/evaluate.py` to compute:
- **Word Error Rate (WER)** for the Whisper transcription step
- **BLEU score** per language for translation
- **Leader election time**: measure and log how long failover takes from leader death to new leader announcement, across a few repeated trials
- **Load balancing effectiveness**: with 2 English workers under simulated load, log chunk distribution between them (should be roughly even)

Results are written to `eval/results.json` for inclusion in your report.
