# Polyglot Relay — Real-Time Multilingual Speech Caption System

A distributed, fault-tolerant pipeline that captures live speech from a speaker, transcribes it in real time, and fans the transcript out in parallel to independent translation worker pools — one pool per target language — so listeners each see live captions in their own language.

Built as a distributed programming coursework project. **This version is explicitly designed to exercise core distributed systems concepts** — leader election, replication, dynamic service registration, and load-balanced worker pools — rather than just wrapping AI APIs in a pipeline.

---

## Why this project

Live multilingual communication (classrooms, community meetings, disaster briefings) usually assumes everyone shares a language or relies on a single human interpreter — a single point of failure and a bottleneck. This project explores a distributed alternative where translation capacity per language can scale independently, and no single component's failure takes down the whole system — including the coordination layer itself.

## Scope (MVP)

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

**Explicitly out of scope for MVP (stretch goals):**
- Text-to-speech output (captions only)
- Multiple simultaneous speakers
- Persistent storage / session history across restarts
- Authentication
- Full Raft-style log replication for broker state (we use a simpler leader-election + state-resync approach — see "Design tradeoffs" below)

---

## Architecture

```
                STT service (audio file or live mic + Whisper)
                                    │
                        discovers current leader
                                    ▼
                ┌───────────────────────────────────┐
                │   Broker A (leader)  ◄──heartbeat──►  Broker B (standby)  │
                │   accepts producer + worker conns  │   watches leader, takes │
                │                                     │   over on failure       │
                └───────────────────┬─────────────────┘
                                    │  registers + fans out
        ┌───────────────┬──────────┼──────────┬───────────────┐
        ▼                ▼                    ▼                ▼
  EN worker pool     ASEAN worker pools (my, id, ms, th, vi,     ...9 pools total
  (2 instances,               tl, km, lo)                        one per language,
   round-robin)                                                  independent & parallel
        │                                                          │
        ▼                                                          ▼
                     Dashboard (or any listener client) shows every live feed
```

### Component responsibilities

- **STT service**: the command-line way captions enter the system. Takes either a WAV file (`--input-file`) or a live microphone stream (default, no flags), buffers audio into short chunks, runs each chunk through a local Whisper model, and sends the transcript to whichever broker is currently the leader (with retry/reconnect logic if the leader changes mid-session). Raw PCM audio is only attached if you pass `--send-audio`; by default only text goes out, since the plain-text translation path is faster and more reliable across many languages than opening a live audio session per chunk (see "Translation modes" below). The web dashboard offers the same audio-in experience from a browser instead — upload an MP3/audio/video file, or record live through the mic (optionally with camera) — transcribed server-side via the same Whisper pipeline; there is no typed-text input anymore.
- **Broker replicas (2)**: only the current **leader** accepts connections from the STT service and translator workers. The **standby** replica exchanges heartbeats with the leader; if it misses enough heartbeats, it triggers a leader election (see below) and promotes itself. Workers and the STT service detect the leader change via failed connection + a shared list of known broker addresses, and reconnect to whichever one is now active.
- **Leader election**: a simplified **Bully algorithm** — each broker replica has a fixed, known ID. On startup or on detecting the leader is unreachable, a replica broadcasts an election message to replicas with higher IDs; if none respond, it becomes leader and announces itself. With only 2 replicas this is simple to reason about and correct to implement, while still being a genuine leader election protocol, not just a hardcoded primary/backup.
- **Dynamic worker registration**: translator workers do **not** appear in any static config. On startup, a worker connects to the current leader broker and sends a `register` message (`lang`, `worker_id`, `host`, `port`). The broker adds it to that language's pool. This means adding capacity for a language is "start another worker process," not "edit a config and restart everything." Adding a *new* language is one line in `common/config.py`'s `LANGUAGES` dict — every other module (broker, workers, dashboard, UI) derives its language list from that single source.
- **Worker pools + load balancing**: the broker keeps a list of registered worker instances per language and distributes incoming transcript chunks **round-robin** across a language's pool. This is real horizontal scaling — you can demonstrate throughput increasing as you add a second English worker under load, distinct from fault isolation (which is about correctness under failure, not capacity under load). Any language can be scaled the same way; English ships with 2 replicas by default to make the demo obvious.
- **Listener clients**: connect to their language's currently-assigned worker instance (discovered via the broker) and render captions live. The web dashboard listens to all 9 pools at once.

## Translation modes

Every worker calls out to a translation backend through `translator_worker.py`'s `Translator` class, which supports a pluggable set of them, plus a zero-cost passthrough. `TRANSLATION_BACKEND` (default `auto`) controls which are tried:

| Mode | When it's used | Trade-off |
|---|---|---|
| **Passthrough** (`passthrough`) | Automatic whenever the caption's declared source language matches a worker's own language (see "Source language" below). | Instant, no API call at all — translating a language into itself would be a no-op anyway. |
| **LibreTranslate** (`libretranslate`) | `auto` (default) or `libretranslate`. Self-hosted, open-source engine — see `compose.libretranslate.yaml`. Tried first if the worker's language is loaded (discovered at runtime, not hardcoded). | Free forever, no quota, no account — confirmed to cover 6 of the 9 languages; Burmese/Khmer/Lao are not in Argos Translate's catalog and always need the Gemini fallback. |
| **Text** (`gemini-text`, model `gemini-pro-latest` on the `paid` tier, `gemini-3.6-flash` on `free`) | `auto` (fallback) or `gemini`. One REST call per caption, using the transcript Whisper already produced. | Fast, accurate, works for all 9 languages — but metered by Google's API quota (see "Rate limits" below, and "Gemini API tier" above for paid- vs free-tier defaults). The system instruction (`translator_worker.py`'s `_translate_text`) frames this explicitly as live spoken-conversation captioning, not document translation: it asks for natural/conversational phrasing over stiff word-for-word translation, and tells the model the fragment may be an incomplete sentence (live chunks often are) rather than something to complete or restructure. |
| **Live** (`gemini-live`, model `gemini-3.5-live-translate-preview`) | Opt-in via `stt_service.py --send-audio`. Re-transcribes and translates directly from raw audio over a duplex session. | Slower and more failure-prone (one live session per chunk, per language, per worker) — not all 9 target languages are guaranteed to be supported by Gemini Live yet. Useful for demonstrating speech-to-speech translation specifically, not for everyday routing. |

Run entirely on LibreTranslate with `docker compose -f compose.yaml -f compose.libretranslate.yaml up --build -d` — no API key needed at all. See `DOCKER.md` for the full walkthrough, including how to add Gemini as a fallback for whatever LibreTranslate doesn't cover.

If translation ever silently stops working (workers show "connected" on the dashboard but no captions arrive), check the worker's logs first — a failed live-audio session is the most common cause, and switching back to the default text-only mode is the fix.

### Source language ("any language to multiple languages")

Every caption can declare which language it's spoken in — the dashboard's composer has a "Speaking in" dropdown (defaults to **Auto-detect**), and `stt_service.py` exposes the same choice via `--source-lang`. This is not just metadata: a worker whose own language matches the declared source skips translation entirely (passthrough, above) instead of assuming every caption starts in English, which is what earlier versions of this project did implicitly. Pick "Auto-detect" and Gemini identifies the source language itself for every target — nothing is hardcoded to English anywhere in the translation path. A concrete pick (not "Auto-detect") is also passed to Whisper itself for audio uploads/recordings (`web_ui.py`'s `submit_audio` → `stt_service.py`'s `transcribe_file`, `language=`) - skipping its own detection pass is both faster and more accurate than guessing, but only helps when the pick is actually correct for that clip; a stale pick left over from a previous, different-language upload will actively hurt accuracy, which is why the dashboard resets to Auto-detect on every page load rather than remembering your last choice.

Every caption card in the dashboard also shows the **original pre-translation text** in a small quoted line above the translation (skipped when it'd just repeat the same text, e.g. passthrough) - `DashboardState.record_original` keeps a bounded (200-entry) map of `chunk_id → original text` alongside the per-language caption history, so you can always see exactly what was actually said, not just the translated result.

### Audio processing (`stt_service.py`'s `transcribe_file`)

An uploaded/recorded file gets transcribed in **one whole-file Whisper pass**, not pre-sliced into fixed-size windows - slicing first (the original approach) cuts sentences mid-word at arbitrary boundaries, which both wastes compute (each slice pays a full padded-to-30s encoder pass regardless of its real length) and hurts accuracy (a truncated fragment often transcribes as empty text, silently dropping that part of the audio). Whisper's own segmentation handles natural sentence/pause boundaries far better than blind fixed-window slicing. Live microphone capture (`transcribe_microphone`) is a deliberate exception - real-time streaming genuinely needs small, bounded-latency chunks, which a finished recording does not.

Two independent signals guard against Whisper **hallucination** (fluently transcribing noise, music, or silence as if it were real speech) beyond just "does this look empty":
1. **Whisper's own confidence**: a segment with high `no_speech_prob` (thinks it isn't speech at all) *and* low `avg_logprob` (wasn't confident in the words either) is dropped - the same thresholds Whisper's own CLI uses.
2. **A second, independent opinion**: `py3langid` (a small, self-contained text-language classifier - no heavy model, no network call) checks whether the transcribed *text* is confidently identifiable as the expected language. A segment that disagrees with high confidence gets dropped too - this catches a different failure mode than #1: text Whisper transcribed fluently and confidently, but in the wrong language entirely.

Whisper's own per-file language detection (when "Auto-detect" is left selected) is propagated forward as the caption's `source_lang`, not left as the literal string `"auto"` - this unlocks the passthrough shortcut for the matching language and the known-source Gemini prompt variant for every other worker, instead of every worker guessing independently. The dashboard also surfaces this back to you: a successful "Auto-detect" upload shows a toast like *"Sent 2 captions to the relay — detected Thai"*.

### Rate limits and retries (why only 3-4 languages used to translate on a free key)

A single caption is broadcast to all 9 language pools by the broker at essentially the same instant (`broker.route_chunk`) — though in practice only Burmese/Khmer/Lao actually call Gemini once LibreTranslate is running (see "Translation modes" above), so this mostly matters if you're forcing `TRANSLATION_BACKEND=gemini`. Google's Gemini free tier enforces a *hard* 5-requests-per-minute ceiling **per project+model, shared across every worker** — not a soft "usually fine" limit, and not per-worker — so firing 9 concurrent API calls at once will always trip it on a free key; only whichever 5 land first succeed, and the rest fail (visible as `429 RESOURCE_EXHAUSTED` in a worker's logs) unless the mitigations below cover the gap. A billing-enabled paid-tier key (`GEMINI_TIER=paid`, the default — see "Gemini API tier" above) raises that ceiling enough that none of this is a concern:

1. **Shared rate gate** (`gemini_rate_gate.py`, `GEMINI_TIER=free` only): every Gemini-backed worker blocks on `GET /acquire` against a small shared HTTP service (`compose.real.yaml`'s `gemini-rate-gate`) before calling Gemini, which enforces the real 5/minute ceiling across *all* workers together via a sliding window — not just spreads out each worker's own guess at when to fire. This is what actually keeps a 9-language free-tier broadcast from mostly 429ing; expect later languages in a burst to visibly lag behind earlier ones (up to the low tens of seconds) rather than error out. Unreachable gate → a logged warning, not a failure; translation falls back to retry-with-backoff alone.
2. **Retry with backoff**: any retryable error (`429`, `503`, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`) is retried up to `GEMINI_MAX_RETRIES` times (default 5) with exponential backoff and jitter, so a transient rate-limit rejection recovers on its own a few seconds later instead of being a permanent failure.
3. **Per-worker stagger**: on `GEMINI_TIER=free`, each worker also sleeps a small, deterministic offset based on its position in `TARGET_LANGUAGES` (about 0.08s more per language, ~0.6s total spread across all 9) before its first attempt on a chunk, ahead of the rate gate above. On the `paid` tier this defaults to `0s`, since the higher RPM ceiling doesn't need it.
4. **Passthrough** (above) also shrinks every burst by one call whenever the caption's source language is known.
5. **Daily quota is a separate, harder cap**: a `429` whose `quotaId` mentions `PerDay` is not retried at all (see `_is_daily_quota_exhausted`), because it will not clear for hours regardless of backoff — this applies on both tiers, just at very different daily ceilings. See "Gemini API tier" above if this keeps happening on a key that's genuinely billing-enabled - it may be a tier-propagation gap on a specific model rather than a real quota problem.

The recommended LibreTranslate-hybrid setup (`compose.libretranslate.yaml`) doesn't wire up `gemini-rate-gate` at all - only Burmese/Khmer/Lao ever reach Gemini there (3 concurrent calls, safely under the free tier's 5/minute ceiling on their own), so retry-with-backoff alone is enough. The gate only matters if you're running `compose.real.yaml` (pure Gemini, all 9 languages) on a free-tier key.

If every single language consistently fails even with the rate gate running, check the worker logs for a non-retryable error (`401`/`403`/invalid key) instead — that's a key problem, not a quota one.

## Distributed programming concepts demonstrated (map this section directly into your report)

| Concept | Where it shows up |

|---|---|
| Leader election | Bully algorithm between the two broker replicas |
| Replication / failover | Standby broker takes over if the leader dies, with workers and the STT service reconnecting transparently |
| Dynamic service registration | Workers register at runtime; no static worker list |
| Load balancing / sharding | Round-robin chunk distribution across a language's worker pool (9 independent pools, English scaled to 2 instances) |
| Failure detection | Heartbeats at two levels — broker-to-broker (for leader liveness) and worker-to-broker (for pool health) |
| Fault isolation | A dead worker only affects its own language; a dead non-leader broker doesn't affect the system at all |

## Tech stack

| Component | Choice |
|---|---|
| Language | Python 3.10+ |
| Speech-to-text | [OpenAI Whisper](https://github.com/openai/whisper) (self-hosted, `base` model recommended, `tiny` as CPU fallback, `small` for better accuracy if you can afford a larger one-time download) |
| Hallucination filtering | Whisper's own confidence signals (`no_speech_prob`, `avg_logprob`) plus [py3langid](https://github.com/adbar/py3langid) as an independent text-language cross-check |
| Translation | Gemini text (`gemini-pro-latest` on a paid key, `gemini-3.6-flash` on free — see "Gemini API tier") for typed transcript text (default); Gemini Live Translate (`gemini-3.5-live-translate-preview`) for speech audio (opt-in) |
| Inter-process communication | Raw Python `socket` (TCP), custom JSON-line protocol |
| Leader election | Custom Bully algorithm implementation over TCP |
| Audio capture | `sounddevice` or `pyaudio` |
| Caption display | Web dashboard (`web_ui.py`) for MVP; terminal `listener_client.py` also included |

## Project structure

```
polyglot_relay/
├── stt_service.py            # CLI: mic/file audio → Whisper → sends to current leader
├── broker.py                 # Broker replica: leader election, registration, fan-out, load balancing
├── translator_worker.py      # Registers with leader; translates; serves listeners
├── listener_client.py        # Terminal client: connects to an assigned worker, displays captions
├── web_ui.py                 # Web dashboard - the primary way most people run this: upload/record
│                              #   audio (transcribed via the same Whisper pipeline), or POST typed
│                              #   text; shows all 9 live caption feeds + network health in one page
├── gemini_rate_gate.py        # Shared sliding-window rate limiter for GEMINI_TIER=free (see
│                              #   "Rate limits and retries") - only wired into compose.real.yaml
├── ui/                        # web_ui.py's static frontend (index.html, app.js, app.css)
├── common/
│   ├── protocol.py           # Message formats (register, heartbeat, election, transcript_chunk, translated_chunk)
│   ├── election.py           # Bully algorithm implementation, shared by broker replicas
│   ├── heartbeat.py          # Heartbeat sender/receiver + failure detection, used at both levels
│   ├── discovery.py          # Leader/worker address discovery over the broker's socket protocol
│   ├── healthcheck.py        # Shared HTTP health-check handler used by the Docker images
│   └── config.py             # Broker replica addresses/IDs, language list, ports, chunk size, timeouts
├── eval/
│   ├── benchmark_sentences.json
│   └── evaluate.py
├── tests/                    # unittest suite (65 tests) - run via `python -m unittest discover -s tests -v`
├── Dockerfile                 # Multi-stage build: mock/translate/full/test targets (see DOCKER.md)
├── compose.yaml                # Base cluster (mock translation, no external deps)
├── compose.libretranslate.yaml # Recommended overlay: free self-hosted LibreTranslate + Gemini
│                                #   fallback for the 3 languages it doesn't cover (see "Translation modes")
├── compose.real.yaml           # Pure-Gemini overlay: all 9 languages through Gemini, no LibreTranslate
├── compose.audio.yaml          # Opt-in overlay to feed a WAV file through the STT container
├── .env.example                 # Copy to .env - GEMINI_API_KEY and the tier/model overrides live here
├── requirements.txt
├── README.md
└── DOCKER.md                  # Full Docker walkthrough: routing, worker-failure, broker-failover demos
```

## Setup

```bash
git clone <your-repo-url>
cd polyglot_relay
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

You'll need:
- A Gemini API key from Google AI Studio, set via `GEMINI_API_KEY`. Ideally one from a **billing-enabled (paid) project** — see "Gemini API tier" below for what that changes.
- `ffmpeg` installed on your system (required by Whisper)
- Either an audio/video file, or a working microphone (optionally a camera)

There is no typed-text input anywhere in this project anymore — every caption comes from Whisper transcribing real audio. Three ways to get it in:
1. **The web dashboard** (`http://localhost:8080`) — upload an MP3/audio/video file, or record live through your browser's microphone (and, if you want, camera). Transcription happens server-side via Whisper; the browser only ever sends the recorded/uploaded bytes over HTTP, so this works the same whether the dashboard is running in Docker or natively — there's no microphone-passthrough issue here, since the actual recording happens in your browser on the host, not inside the container.
2. **`stt_service.py --input-file <path>`** — a WAV/MP3/any Whisper-readable file, works natively or via `compose.audio.yaml` in Docker.
3. **`stt_service.py` with no flags** — live microphone capture via `sounddevice`, but this one genuinely needs to run natively on your host: Docker Desktop (particularly on Windows) can't reliably pass a physical microphone through to a Linux container the way a browser can.

### Gemini API tier

`GEMINI_TIER` (default `paid`) tells every worker what kind of `GEMINI_API_KEY` it's holding, and picks defaults accordingly — an explicit `GEMINI_TEXT_MODEL` or `POLYGLOT_STAGGER_STEP_SECONDS` always wins over either tier's default:

| Tier | `GEMINI_TEXT_MODEL` default | Per-worker stagger | When to use it |
|---|---|---|---|
| `paid` (default) | `gemini-pro-latest` — higher translation quality | none (`0s`) | A Google AI Studio / Cloud key with billing enabled. Paid-tier requests-per-minute quota comfortably absorbs all 9 workers calling Gemini at the same instant, so the free-tier stagger workaround is unnecessary, and the higher-quality Pro model is affordable to run by default. |
| `free` | `gemini-3.6-flash` — cheaper, faster | ~0.08s × language position (~0.6s spread across all 9) | A free Google AI Studio key with no billing account attached. Keeps the original rate-limit-safe behavior described below. |

Set it once before starting your workers:

```bash
export GEMINI_TIER=free   # only if your key is NOT billing-enabled
```

(On Windows PowerShell: `$env:GEMINI_TIER = "free"`.) Leave it unset to use the `paid` default.

**If a billing-enabled key still gets `429`s tagged `-FreeTier`** (visible in a worker's logs, and in Google's own error payload's `quotaId`) despite Google Cloud Console and `ai.dev/rate-limit` both showing paid-tier limits for the project: this is a real, observed inconsistency between what Google's billing/tier system shows as active and what a *specific model* actually enforces server-side - the tier upgrade can propagate to some models before others. Confirmed on a live key where `gemini-3.6-flash` stayed stuck on free-tier enforcement (`limit: 20`/day, `limit: 5`/min) while `gemini-3.7-flash` correctly reflected the paid tier with no such errors. If you hit this, try pinning `GEMINI_TEXT_MODEL` to a different (typically newer) model name than the tier default before assuming the account itself is broken - it may simply be that specific model version that hasn't caught up yet.

## Running the system

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

Speak into your microphone (or point `--input-file` at a WAV) — captions should appear per language within a few seconds. The `--source-lang` flag lets you declare which ASEAN language the speaker is using instead of relying on auto-detection (e.g. `--source-lang th` for a Thai speaker) — see "Source language" below.

**Demo script for the fault-tolerance story:**
1. Kill the leader broker process (`Ctrl+C`). Show the standby detect it within ~6-8 seconds, run the election, and become the new leader — workers and STT service reconnect automatically, pipeline keeps running.
2. Kill one of the two English translator instances mid-session. Show captions keep flowing for English (the remaining instance picks up all the load) and every other language pool is completely unaffected.
3. Start a fresh second English worker mid-session and show it gets registered and starts receiving a share of the load without restarting anything else.

## Evaluation

Run `python eval/evaluate.py` to compute:
- **Word Error Rate (WER)** for the Whisper transcription step
- **BLEU score** per language for translation
- **Leader election time**: measure and log how long failover takes from leader death to new leader announcement, across a few repeated trials
- **Load balancing effectiveness**: with 2 English workers under simulated load, log chunk distribution between them (should be roughly even)

Results are written to `eval/results.json` for inclusion in your report.

## Design tradeoffs (be upfront about these in your report)

- **Bully algorithm over Raft**: Raft is the more "complete" consensus algorithm (handles log replication and multiple failure scenarios more rigorously), but is significantly more implementation effort for a 2-replica broker. Bully election gives you a real, correct leader-election mechanism without that overhead — a reasonable engineering tradeoff for this scope, and worth explicitly naming as a design decision rather than an oversight.
- **No persistent broker state**: if both broker replicas die simultaneously, in-flight registration state is lost and workers must re-register on reconnect. This is an accepted limitation for MVP scope — a production system would persist registration state, not just hold it in memory.
- **Round-robin, not load-aware, balancing**: chunks are split evenly by count, not by each worker's actual current load. A more sophisticated version would track per-worker latency and route accordingly — noted as a stretch goal.

## Stretch goals (if time allows)

- Load-aware (not just round-robin) balancing across worker pools
- Text-to-speech output per language
- Web-based listener UI instead of terminal
- A third broker replica to demonstrate election with more than 2 candidates

## Local smoke test (no cloud account or microphone)

The implementation includes deterministic mock translation and stdin transcript
input so the distributed behavior can be demonstrated before installing the
optional ML/cloud dependencies:

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

Type one caption per line in the STT terminal. For real audio and translation,
install `requirements.txt`, configure `GEMINI_API_KEY`, and run
workers without `--mock`.

The selected worker sends its completed translation back through the leader,
which relays it to every worker in that language pool. This keeps translation
load-balanced while ensuring a listener connected to any pool member receives
the complete caption stream.

### Gemini translation modes

Real workers use the current `google-genai` SDK. Audio captured from a WAV file
or microphone is carried alongside the Whisper transcript as 16 kHz PCM and is
translated through `gemini-3.5-live-translate-preview`. The worker streams the
audio in 100 ms frames and publishes Gemini's output transcription as the live
caption. Text submitted through the dashboard or `--stdin` cannot be sent to
Live Translate because that model accepts audio only, so it is translated with
the text model instead — `gemini-pro-latest` by default (`GEMINI_TIER=paid`) or
`gemini-3.6-flash` if `GEMINI_TIER=free`. Both model names can be overridden
directly with `GEMINI_LIVE_MODEL` and `GEMINI_TEXT_MODEL`, which always win
over the tier default.

The round-robin design treats each 2.5-second audio chunk as an independent Live
API session. This preserves horizontal worker balancing; a continuous
speech-to-speech session would instead need sticky routing to one worker.

Run automated checks with `python -m unittest discover -s tests -v`, and generate
evaluation output with `python eval/evaluate.py`.

## Docker

The repository includes a multi-stage `Dockerfile` and several Compose overlays for different translation setups. For the complete run, routing, worker-failure, and broker-failover walkthrough, see [DOCKER.md](DOCKER.md).

**Quick smoke test (no API key, no real translation)** - `docker compose up --build` alone uses only the base `compose.yaml`, which is **mock mode**: every language just shows `[lang] <original text>` instead of an actual translation. Good for verifying the distributed-systems plumbing (election, failover, load balancing) works without any external dependency, not for demonstrating real translation.

**Recommended for real translation** (free, no Gemini API key required for 6 of 9 languages):
```bash
docker compose -f compose.yaml -f compose.libretranslate.yaml up --build -d
```
This adds a self-hosted LibreTranslate service for English/Indonesian/Malay/Thai/Vietnamese/Filipino, with Gemini as the fallback only for the 3 it doesn't cover (Burmese/Khmer/Lao - see "Translation modes" above). Set `GEMINI_API_KEY` in `.env` (copy from `.env.example`) to enable that fallback; without a key, those 3 languages simply won't translate while the other 6 work normally.

**Pure Gemini, all 9 languages** (needs a `GEMINI_API_KEY`, and ideally a billing-enabled one - see "Gemini API tier" above):
```bash
docker compose -f compose.yaml -f compose.real.yaml up --build -d
```

Whichever overlay you use, open [http://localhost:8000](http://localhost:8000) for the interactive control room: upload an audio/video file or record live through your microphone, run a built-in demo, inspect all 9 live language feeds, and monitor broker and worker health from one screen. Bring the stack down with `docker compose -f compose.yaml -f <overlay>.yaml down` (matching whichever overlay you started with).

Note the dashboard (`ui`) always builds from the `full` Docker target regardless of overlay, since it needs Whisper/ffmpeg to transcribe uploads/recordings
server-side — expect a noticeably longer first build than before.
