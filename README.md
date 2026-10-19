# Polyglot Relay

**One voice, every channel.** A distributed, fault-tolerant pipeline that transcribes live speech and fans it out — in parallel, in real time — to independent translation worker pools, one per language, so every listener gets live captions in their own tongue.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Languages](https://img.shields.io/badge/languages-9%20ASEAN-e8a63c)
![Tests](https://img.shields.io/badge/tests-65%20passing-2ea44f)
![Docker](https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white)

Built as a distributed-systems coursework project — leader election, replication, dynamic service registration, and load-balanced worker pools, not just an AI API wrapped in a pipeline.

---

## Why

Live multilingual communication — classrooms, community meetings, disaster briefings — usually depends on everyone sharing a language, or on a single human interpreter: a bottleneck and a single point of failure. Polyglot Relay explores the distributed alternative: translation capacity per language scales independently, and no single component's failure — including the coordination layer itself — takes the whole system down.

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

Two broker replicas run a real **Bully leader-election** protocol; translator workers **dynamically register** with whichever is currently leader — nothing is hardcoded, and a language's worker pool is **round-robin load-balanced** with zero-downtime failover at both levels. Full breakdown in [docs/DETAILS.md](docs/DETAILS.md).

## Features

- **9 ASEAN languages** in parallel — English, Burmese, Indonesian, Malay, Thai, Vietnamese, Filipino, Khmer, Lao
- **Pluggable translation backends** — free self-hosted LibreTranslate first, Gemini fallback for what it doesn't cover, with passthrough for free when source = target
- **Whisper transcription** with hallucination filtering (two independent confidence signals, not just "does this look empty")
- **Self-healing** — dead leader → automatic election; dead worker → its pool keeps running; both invisible to listeners
- **Web dashboard** — upload/record audio or send typed text, watch all 9 language feeds and cluster health live
- **Listener page** — `/listen` shows every sentence in all the languages you pick at once, and can read one aloud
- **At-least-once delivery** — a caption is never lost to a dead worker or broker, even if every broker restarts (jobs are journalled to disk)
- **Rooms** — many separate audiences on one relay (`?room=hall-a`), each with its own captions and history
- **Push updates** — captions reach browsers the moment they are translated (Server-Sent Events), not on the next poll
- **Translation with context and a glossary** — the previous sentences and an event glossary keep names and terms consistent
- **Raft-style election (optional)** — majority votes and terms, so a network split can never produce two leaders
- **Authenticated cluster** — a shared secret (HMAC) that every broker, worker and listener must prove
- **Monitoring** — Prometheus metrics and a ready-made Grafana dashboard: delay per language, queues, redeliveries, leadership
- **Autoscaling** — worker pools grow and shrink with their queues
- **Over 1,000 automated tests** (plus 138 for the browser scripts), CI on every push, load and chaos tests

## Quick start

**Selected source → eight translations, chunk by chunk:** see
[the current chunk workflow and free-tier run guide](docs/CHUNK_WORKFLOW.md).
It covers the dashboard's progress table, worker queues, broker monitoring,
API limits, and the distinction between failover and durable message recovery.

**No API key, real translation, 6 of 9 languages free forever:**
```bash
docker compose -f compose.yaml -f compose.libretranslate.yaml up --build -d
```
Open **[http://localhost:8000](http://localhost:8000)**. Add a `GEMINI_API_KEY` in `.env` (copy from `.env.example`) to also cover the 3 languages LibreTranslate doesn't have (Burmese/Khmer/Lao).

**Fastest possible smoke test** (mock translation, no dependencies at all):
```bash
docker compose up --build
```

Full Docker walkthrough — including a pure-Gemini overlay, worker-failure and broker-failover demos — in [DOCKER.md](DOCKER.md).

## Tech stack

| | |
|---|---|
| Language | Python 3.10+ |
| Speech-to-text | [OpenAI Whisper](https://github.com/openai/whisper), self-hosted |
| Translation | [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) (self-hosted) + [Gemini](https://ai.google.dev/) (fallback) |
| Hallucination filtering | Whisper's own confidence scores + [py3langid](https://github.com/adbar/py3langid) text-language cross-check |
| Networking | Raw Python `socket` (TCP), custom JSON-line protocol — no message broker library |
| Leader election | Custom Bully algorithm over TCP |
| Frontend | Vanilla HTML/CSS/JS dashboard (`web_ui.py` + `ui/`) |

## Project structure

```
├── stt_service.py             # CLI: mic/file audio → Whisper → sends to current leader
├── broker.py                  # Broker replica: leader election, registration, fan-out, load balancing
├── translator_worker.py       # Registers with leader; translates; serves listeners
├── listener_client.py         # Terminal client: connects to an assigned worker, displays captions
├── web_ui.py + ui/            # Web dashboard — the primary way to run this
├── history.py                  # Caption history: SQLite store per room, exports (text, CSV, JSON, SRT)
├── glossary.py                 # Event glossary and translation context
├── fragment_guard.py           # Drops transcripts invented for silence or music
├── job_journal.py              # Unfinished jobs on each broker's disk
├── metrics.py                  # Prometheus metrics (served at /metrics)
├── tts.py                      # Read-aloud: Gemini speech, disk cache, voices, daily allowance
├── monitoring/, compose.monitoring.yaml   # Prometheus + Grafana dashboard
├── tools/                      # smoke_test, load_test, chaos_test, autoscale
├── gemini_rate_gate.py         # Shared rate limiter for Gemini's free tier
├── common/                     # Protocol (+ HMAC auth), Bully and Raft election, heartbeat, discovery, config
├── eval/                       # Metrics (CER/WER/chrF++ with bootstrap CIs), FLEURS-style runner, failover trials
├── tests/                      # 400+ test suite (unittest + Node), ~93% line/branch coverage
├── Dockerfile, compose*.yaml   # Multi-stage build + overlays (mock / LibreTranslate+Gemini / pure Gemini)
└── docs/DETAILS.md             # Full technical deep dive + report-ready sections
```

## Setup (native, no Docker)

```bash
git clone https://github.com/winpyishanhtat/polyglot-relay.git
cd polyglot-relay
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

You'll need a [Gemini API key](https://aistudio.google.com/apikey) (set via `GEMINI_API_KEY`), `ffmpeg` on your system, and either an audio file or a microphone. Three ways to get audio in: the web dashboard, `stt_service.py --input-file`, or live mic capture — see [docs/DETAILS.md](docs/DETAILS.md) for the full manual multi-terminal walkthrough and the mock-mode smoke test.

## Running it for other people

The defaults suit one person on one machine. Before others can reach it:

1. Set `POLYGLOT_ACCESS_TOKEN` in `.env`. Anyone can then *watch* captions, but submitting text,
   uploading audio, going live and clearing need the token (sent as `X-Access-Token`). Give the
   presenter a link ending `?token=YOUR_TOKEN`; the page remembers it for that tab.
2. Put an HTTPS reverse proxy (Caddy, nginx) in front of port 8000. Browsers only allow the
   microphone on `https://` or `localhost`.
3. Keep `POLYGLOT_RATE_LIMIT_PER_MINUTE` (default 240 changes per client address) and set a spend
   cap and key restriction in Google Cloud. If the proxy hides client addresses, limit there too.
4. Set `POLYGLOT_CLUSTER_SECRET` (see "Security: the cluster secret"), so only processes that know it can
   join the relay. The broker (7001-7003) and LibreTranslate (5001) ports are still published on
   `127.0.0.1` only; keep it that way.
5. Audio and text are sent to the Gemini API: tell speakers and check Google's current terms for
   your tier. Captions are held in memory only unless you set `POLYGLOT_HISTORY_FILE` (see History).
6. To make *viewing* private too, set `POLYGLOT_VIEW_TOKEN`. Every page and read then needs it, and
   `/health` stays open. Share listener links as `https://HOST/listen/th?token=THE_TOKEN`; the first visit
   stores it in an HttpOnly cookie. The access token also opens the room, so the presenter can watch it,
   but a viewer's token never allows changes. With both set, viewers read and listen, the presenter changes.

## Rooms

One relay can serve many separate audiences. Add `?room=NAME` to the dashboard address (or type a name in the
**Room** box at the top): that room has its own captions, chunk table, live microphone, history and exports,
and its own listener page (`/listen?room=NAME`). Names are up to 32 lowercase letters, digits or dashes; the
default room is `main`. Rooms are created on first use, up to `POLYGLOT_MAX_ROOMS` (default 20), and rooms
with saved history come back after a restart. The brokers and workers do not know about rooms: the room
travels in the chunk id (`hall-a~ui-...`), so every room shares the same worker pools and failover. Every API
takes the `room` parameter (`/api/history?room=hall-a`, `POST /api/captions?room=hall-a`, ...), and
`/api/rooms` lists them.

## Push updates

Browsers keep one connection open to `/api/events?room=NAME` (Server-Sent Events) and the dashboard pushes
every new sentence, translation, activity line and worker change the moment it happens, so captions appear
as soon as a worker answers instead of on the next once-a-second poll. A browser that reconnects sends the last
event it saw and gets what it missed; one that has fallen too far behind is told to reload. While the stream
is open the pages poll only as a safety net (the dashboard every 4 s for network status, the listener page
every 10 s); without it (an old browser, a proxy that buffers) they poll every second as before. Behind nginx,
turn off proxy buffering for `/api/events` (the server already sends `X-Accel-Buffering: no`).

## Listener page and read-aloud

`/listen` shows what is being said in **several languages at the same time**: each spoken sentence appears
once, with its translation in every language you have switched on underneath (side by side on a wide screen,
stacked on a phone), filling in as each language arrives, with the previous sentences below. All nine are on
by default; the language buttons switch any of them off or on and the choice is remembered. Share a link that
starts with fewer: `/listen?langs=th,my,km`, or `/listen/th` for just Thai. **A−/A+** resizes the text.
**Read aloud** speaks each new caption in the language you choose beside it (it skips ahead rather than fall
behind). The browser's own voice is used when it has one for the language; otherwise the page asks
`POST /api/tts`, which returns a WAV made by Gemini speech (`GEMINI_TTS_MODEL`, `GEMINI_TTS_VOICE`; repeated
phrases are cached). Browsers rarely have
Burmese, Khmer or Lao voices, so those usually use the relay's. Free-tier keys allow only about 10 speech
requests per day per model, and the page shows Google's message when that runs out. Every caption card on the
main dashboard also has a **Listen** button. `/api/tts` needs a token when one is set, and is rate limited
like every other POST.

- **Voices:** beside **Read aloud** a voice list offers the device's own voices for the language and the
  relay's Gemini voices; the choice is remembered per language. `GEMINI_TTS_VOICE_TH` (any language code) sets
  the relay's default voice for one language.
- **Clips on disk:** with `POLYGLOT_TTS_CACHE_DIR` (the Compose stack uses `/data/tts`) a phrase is synthesised
  once and kept, up to `POLYGLOT_TTS_CACHE_MB` (default 200 MB, oldest unheard first).
- **The daily allowance:** every Gemini speech call is counted per model per Pacific day (Google's quota day).
  Set `POLYGLOT_TTS_DAILY_LIMIT` to your allowance (10 on the free tier) and the listener page warns when only a
  few relay-voice clips are left, and says so when they are gone instead of failing; once Google answers that a
  model's daily quota is used up, that model is not called again until the reset. `GET /api/tts/info` reports
  the voices and what is left.
- **Words while speaking:** with `POLYGLOT_INTERIM=1` the dashboard sends the phrase heard so far every 2.5 s
  while someone is talking, and the transcript appears in grey on the dashboard and under the newest sentence
  on the listener page, replaced by the real caption when the phrase ends. Each preview costs a transcription
  (a Gemini call for Burmese, Khmer, Lao and anything Gemini-first), so it is off by default; previews are
  skipped whenever finished speech is waiting.

## Brokers and failover

The Compose stack runs **three broker replicas** (`POLYGLOT_BROKER_COUNT=3`, ids 1-3; the highest live id
leads, by Bully election), so it keeps working after losing two of them in turn. Measured on the live stack,
stopping the leader gave a new one in 2.5 s, then 4.3 s for the next, with every worker staying connected.
The defaults detect a dead leader after three missed 1 s heartbeats; for faster detection set
`POLYGLOT_HEARTBEAT_INTERVAL=0.5` and `POLYGLOT_HEARTBEAT_TIMEOUT=2` (about 3 s, but it can misfire when the
host is starved of CPU). Name lookups for a stopped container take about 4 s in Docker, so every connection
to a broker or worker goes through `common/netutil.py`, which puts a deadline on the lookup and remembers
failures for a few seconds; without it a dead broker froze the dashboard and slowed re-registration.

### Raft-style election (optional)

`POLYGLOT_ELECTION=raft` replaces Bully with Raft's election rules: terms, at most one vote per broker per
term, and leadership only with votes from a majority of all brokers. Two leaders can then never be elected in
the same term, a broker cut off in a minority can never be elected, and a leader that cannot reach a majority
for `POLYGLOT_HEARTBEAT_TIMEOUT` steps down and stops taking work, so the minority side of a network split goes
quiet rather than translating into the void. Every message carries the term, and replication from an older
term is ignored, so a deposed leader cannot overwrite the new one's work. The term and vote are kept on disk
(`POLYGLOT_RAFT_STATE`, `/data/raft-{id}.json` on Docker) so a restarted broker cannot vote twice. The price is
availability: with three brokers the relay survives one failure, not two (use five brokers for two). Only the
election is Raft; jobs are still replicated as described below, not through a Raft log. Bully stays the default
because it keeps a lone surviving broker working.

### Jobs on disk

Each broker keeps unfinished jobs in its own SQLite journal (`POLYGLOT_JOB_JOURNAL`, on by default in Docker at
`/data/jobs-{id}.db`; empty turns it off). The leader writes a job when it hands it out and deletes it when it
is finished or given up; standbys mirror the leader's journal through replication. A broker that starts loads
what is left, so even if **every broker stops at once** the next leader delivers the unfinished jobs again
(those younger than `POLYGLOT_JOB_JOURNAL_MAX_AGE`, default 600 s). Duplicates are ignored downstream.

### Delivery guarantees

Every translation job is delivered **at least once**, and a failure never costs a caption:

- The leader tracks each (chunk, language) job until a worker answers. If the worker disconnects, reports a
  failure or sits on the job past `POLYGLOT_JOB_TIMEOUT`, the job goes to another worker for that language
  (up to `POLYGLOT_MAX_DELIVERY_ATTEMPTS` tries). Jobs for a language with no worker wait, up to
  `POLYGLOT_WAITING_MAX_AGE` seconds, and are delivered when one connects.
- The leader mirrors in-flight and waiting jobs to the standby brokers, so a new leader replays them.
- The speech-to-text side keeps every chunk until the leader acknowledges it (after routing and mirroring), and
  sends it again to the next leader if the connection drops first.
- Workers and the dashboard ignore duplicates, so a redelivered job is never shown twice.
- Work goes to the worker with the fewest outstanding jobs for a language.
- Repeated sentences are answered from a per-worker cache (`POLYGLOT_TRANSLATION_CACHE`, entries, 0 = off).
- A broker that finds itself demoted drops its clients, and a leader yields to a higher live broker, so a
  returning broker never leaves two leaders.

## Security: the cluster secret

Set `POLYGLOT_CLUSTER_SECRET` (the same value for every container; Compose passes it through) and every
message on the relay's own TCP protocol carries a proof that the sender knows it: an HMAC-SHA256 over a
timestamp, a one-time nonce and the message type. The secret itself never travels. A broker checks the first
message of every connection and refuses anything missing, wrong, older than two minutes, or replayed; a
worker does the same for caption listeners, which first send a short `subscribe`. So a process on the same
network that does not know the secret cannot register as a worker, inject captions, read a language's stream
or disrupt an election. It authenticates, it does not encrypt: keep the ports on a private network (or put the
cluster behind a VPN/TLS tunnel) if the captions themselves are sensitive.

## Translation context and glossary

- **Context:** each sentence is sent with the one or two said just before it in the same room
  (`POLYGLOT_CONTEXT_SENTENCES`, default 2, 0 = off). Gemini is told they are context only, to keep names,
  pronouns, gender and tense consistent, and translates only the new sentence. LibreTranslate ignores it.
  The translation cache ignores context on purpose, so a repeated phrase is still free.
- **Glossary:** `POLYGLOT_GLOSSARY_FILE` names a JSON file of terms that must always be rendered the same
  way (see `glossary.example.json`): `"UIT": "*"` keeps a name as written in every language,
  `"broker": {"th": "..."}` fixes one language's rendering. Gemini is given the renderings of the terms in each
  sentence; for LibreTranslate the terms are swapped for placeholders it leaves alone and put back afterwards
  (and if it mangles a placeholder the sentence is translated plainly instead). The file is re-read when it
  changes, so terms can be added during an event. In Docker, copy it into the project before building, or
  mount it (`- ./glossary.json:/glossary.json:ro` on the workers) and set `POLYGLOT_GLOSSARY_FILE=/glossary.json`.

## Speech-to-text: voice activity and invented fragments

- **Voice activity:** the live microphone is cut at pauses, as before, with three refinements: a sound must
  last about 100 ms to count as speech (a click or a knocked table does not open a slice); once speech has
  started it keeps counting down to 60% of the threshold, so soft word endings are not taken for the pause;
  and a reading counts as voice only when most of its energy is in the voice band (300-3400 Hz), so a fan,
  hum or rumble is not sent for transcription.
- **Invented fragments:** transcribers fill silence and music with stock phrases ("Thanks for watching!",
  "Subtitles by the Amara.org community", Vietnamese "Hãy subscribe cho kênh Ghiền Mì Gõ…") or loop on a word.
  `fragment_guard.py` drops a transcript that is *entirely* such a phrase, a courtesy like "Thank you" only
  when the audio was quiet, and runaway repetition. Measured before use on FLEURS
  (`python -m eval.fragment_guard_eval`, `results/fragment_guard.json`): of 3,597 real sentences (all 1,350
  references and every Gemini and Whisper transcript, nine languages) it drops **none**, even treating every
  one as quiet audio, and it catches all 22 fragments in the test list (the list it was written against, so
  that figure shows coverage, not an independent measurement). `POLYGLOT_FRAGMENT_GUARD=0` turns it off.

## Monitoring

```bash
docker compose -f compose.yaml -f compose.libretranslate.yaml -f compose.monitoring.yaml up -d
```

adds Prometheus (http://localhost:9090) and Grafana (http://localhost:3000, the **Polyglot Relay** dashboard
opens without logging in). The dashboard serves everything at `/metrics` in Prometheus format: delay from
speech to caption per language (a histogram), captions per minute, queue depth and jobs waiting for a worker,
job outcomes counted by the leader (routed, completed, redelivered, replayed, failed), each worker's
translations, failures and cache hits, which brokers are up and which leads, rooms, open push streams and
speech calls. With a viewing token, Prometheus sends it as a bearer token (see `monitoring/prometheus.yml`).

## Scaling worker pools

Every worker advertises its own address (`--host auto`) and picks its own id, so any pool can run several
copies: `docker compose ... up -d --scale worker-th=3`. The leader gives each job to the least-loaded copy.
`tools/autoscale.py` does it automatically from `/metrics`: one more worker when a pool's backlog per worker
stays above `--high` (default 3) for two readings, or at once when work waits for a language with no worker;
one fewer after `--cooldown` seconds (default 60) without a backlog; always within `--min`..`--max`.
`--dry-run` only prints what it would do. On this project's Windows machine, add `--clean-env` so the old
`GEMINI_API_KEY` in the shell does not reach new containers.

## History

The live caption cards show the latest 50 captions per language and keep their scroll position. The
**History** panel keeps everything said and translated (up to `POLYGLOT_HISTORY_CHUNKS`, default 2000
chunks, optionally limited by age with `POLYGLOT_HISTORY_MINUTES`). Choose a time frame (last 5, 15 or 60
minutes, the whole session, or a custom from/to), the languages to read, and a search term, then export what
you see as text, CSV (opens in Excel with Burmese, Thai and Khmer intact), JSON or SRT subtitles. The same
data is available at `/api/history` and `/api/export` (`since`, `until`, `last_minutes`, `langs`, `q`,
`limit`, `format`, `lang`). By default history is held in memory only: a restart or **Clear captions &
history** empties it. Set `POLYGLOT_HISTORY_FILE` (the Compose stack mounts a volume at `/data`, so
`/data/history.db` works) to keep it in SQLite across restarts, one table for every room. Memory still holds
the newest `POLYGLOT_HISTORY_CHUNKS` per room for the live view, while the database keeps up to
`POLYGLOT_HISTORY_DB_ROWS` per room (default 100,000) and the History panel's time frames and search run
against all of it. A history file from an earlier version (JSON lines, at the same path or as `history.jsonl`
beside `history.db`) is imported once and kept as `.bak`. A disk error is logged once without stopping
captions, and **Clear captions & history** empties the room in the database too. The file holds everything
that was said, so treat it as private.

## Evaluating it

`eval/` measures quality on real recordings rather than synthesized speech. Build a manifest
(one JSON object per line: `id`, `lang`, `audio`, `transcript`, optional `english`; FLEURS has this
shape for Burmese, Khmer, Lao and the other languages here), then:

```bash
# download the first 150 test clips per language from FLEURS (CC-BY-4.0) and write the manifest
python -m eval.prepare_fleurs --out data/fleurs --clips 150
python -m eval.fleurs_eval asr --manifest data/manifest.jsonl --out results/asr.json
python -m eval.fleurs_eval mt  --manifest data/manifest.jsonl --out results/mt.json --from-asr results/asr.json
python -m eval.failover_trials --trials 30 --out results/failover.json
```

`--stt default|gemini|whisper` chooses the recogniser: `default` is what the relay does (Gemini first for every
language, Whisper as the fallback where it can read the language), `gemini` is Gemini only, `whisper` is
Whisper wherever it can read the language. `prepare_fleurs` streams only the start of each language's test archive (about 100 MB per language, not
the 250-650 MB archive), so each language is scored on its own clips. Run the evaluation inside the `full`
image (it has Whisper and ffmpeg) and only two Whisper containers at a time: five exceed Docker Desktop's
default 3.5 GB. `python -m eval.summarize_results --results results/` merges the per-language files
into one table.

`asr` reports character and word error rate, empty outputs, seconds per clip and real-time factor;
`mt` reports chrF++ against the human English translation. Both give 95% bootstrap intervals, and
a clip that produced nothing scores as a total miss. chrF++ matches sacrebleu to 1e-14.
To measure whether translation context or a glossary helps, run `mt` twice, with and without
`--context 2` (or `--glossary FILE`), and compare: `python -m eval.compare_mt results/mt_plain.json
results/mt_context.json` gives chrF++ per language for both runs and a paired-bootstrap 95% interval for the
difference. (FLEURS sentences are ordered by sentence id, which keeps an article's sentences together.)
COMET is not included: it needs a 2 GB neural model and PyTorch; chrF++ is computed exactly as sacrebleu does.

Measured on 2026-10-19 (`results/context_ab.json`): 60 FLEURS sentences per language, each translated to
English by Gemini without and with the two previous sentences. The differences were between -0.9 and +0.9
chrF++ and **none was significant** (every 95% interval contains 0; e.g. Burmese 56.35 vs 55.77, Vietnamese
62.86 vs 63.76). FLEURS sentences are mostly independent of each other, so this shows that context does no
harm; the consistency it is meant for (names, pronouns, gender carried across sentences of one talk) needs a
test set of continuous speech to measure.

`failover_trials` kills the leader N times and reports the distribution of time-to-new-leader
(4.75 s, SD 0.02 s over 10 trials with the default 1 s heartbeat / 3 s timeout; the earlier 2 s / 6 s
defaults took 8.12 s). `--brokers 3 --kills 2` kills the leader twice in a row (4.7 s, then 4.0 s).

## Testing

```bash
python -m unittest discover -s tests -v
```

or inside Docker: `docker compose --profile test run --rm tests`. The Docker image is stdlib-only, so
tests that need numpy, the Gemini SDK or Node are skipped there; run the suite natively to execute
everything. **GitHub Actions** (`.github/workflows/ci.yml`) runs on every push: the Python and JavaScript
suites, the suite inside the Docker test image, a smoke test and a short load test of a whole relay started
with Compose (mock translators, `compose.ci.yaml`), and a chaos test.

Browser-side tests run the real `ui/app.js` against a fake DOM with Node's built-in runner
(`node --test tests/js/*.test.js` on a shell that expands the pattern, or list the four files; they are also part of the Python run when `node` is installed).

| Area | Test files |
|---|---|
| Protocol, config, election, heartbeat, discovery, health check, listener client, name lookups | `test_protocol.py`, `test_common_modules.py`, `test_netutil.py` |
| Broker: registry, handshake, routing, job telemetry, reaper, leadership | `test_broker.py`, `test_broker_server.py`, `test_cluster.py` (real processes) |
| Translator worker: retry rules, Gemini text/Live calls, LibreTranslate, English pivot, worker process | `test_languages.py`, `test_gemini_translation.py`, `test_worker_runtime.py` |
| Speech-to-text: Whisper windows and filters, Gemini transcription, producer, CLI | `test_stt_service.py`, `test_stt_runtime.py`, `test_chunk_pipeline.py` |
| Dashboard server: state, validation, listeners, broker status, HTTP routes, uploads, live slices | `test_web_ui.py`, `test_web_ui_extra.py` |
| Caption history: store, time/language/text filters, exports, routes | `test_history.py` |
| History on disk, viewing token, listener pages, read-aloud endpoint and speech module | `test_web_features.py` |
| Delivery: redelivery, waiting jobs, replication, balancing, acknowledgements, caching | `test_delivery.py`, `test_acks.py`, `test_translation_cache.py` |
| Dashboard page, listener page (logic and DOM), speech helper and live slicer (JavaScript) | `test_ui_app.py` + `tests/js/`, `test_live_slicer.py` |
| Retry limits, shutdown, command-line entry points, English-pivot decision | `test_edges.py` |
| The smoke-test tool, against stub relays and a real mock cluster | `test_smoke_tool.py` |
| Rooms, SQLite history, push stream, translation context | `test_rooms_and_events.py` |
| Glossary and context in the translator | `test_glossary.py` |
| Cluster secret: signing, replay, brokers and workers refusing outsiders, a real cluster | `test_cluster_auth.py` |
| Raft election (including a randomised split-brain check and a real 3-broker run) and the job journal | `test_raft_and_journal.py` |
| Metrics, the /metrics page and the Grafana dashboard | `test_metrics.py` |
| Read-aloud disk cache, voices and daily allowance | `test_speech_extras.py` |
| Interim captions, the fragment guard and its FLEURS measurement | `test_interim.py`, `test_fragment_guard.py` |
| Autoscaler, load test and chaos test tools (the load test also against a real relay) | `test_autoscale.py`, `test_load_and_chaos_tools.py` |
| Rate gate, evaluation metrics, evaluation runner, failover trials | `test_gemini_rate_gate.py`, `test_gate_and_eval.py`, `test_evaluation.py`, `test_eval_harness.py` |

### Checking a running system

`python tools/smoke_test.py` (add `--url http://HOST:8000` and `--token ...` as needed) sends one typed caption
through the live pipeline and checks that exactly one broker leads, every language has a connected worker, the
dashboard and listener pages are served, all nine translations come back, and the history and export contain the
caption. It prints PASS/FAIL for each check and exits non-zero if any failed, so it can gate a deployment. Add
`--speech` to also test read-aloud (it spends Gemini speech quota). The caption stays in the history until you
use **Clear captions & history**.

`python tools/load_test.py --rate 2 --count 120 --rooms 4` measures throughput and delay per language (median,
95th percentile, worst) and lists anything that never arrived. `python tools/chaos_test.py --duration 120`
does the same while killing a random broker or worker container every 15 s (and starting it again), and
checks the relay's two promises: every caption arrives in every language, and there is never more than one
leader; it also reports the longest time without a leader. Run it on a test stack: it really kills containers.

To see coverage: `pip install coverage`, then
`python -m coverage run --branch --source=. --omit="tests/*,tools/*" -m unittest discover -s tests`
and `python -m coverage report -m`.

## License

Released under the [MIT License](LICENSE). Anyone may use, copy, modify and distribute it, including
commercially, as long as the copyright notice stays with it.

It works with other software that has its own terms, none of which is bundled here:
[LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) (AGPL-3.0, run as a separate
service over HTTP), OpenAI Whisper (MIT), the `google-genai` SDK (Apache-2.0), and the Google Gemini
API, whose use is governed by Google's terms of service and your own API key.

---

For the deep dive — component-by-component design rationale, rate-limit/quota troubleshooting, design tradeoffs, and the distributed-systems concept mapping for your report — see **[docs/DETAILS.md](docs/DETAILS.md)**.
