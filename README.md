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
- **65 automated tests** covering the protocol, election, translation routing, and the dashboard

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
├── gemini_rate_gate.py         # Shared rate limiter for Gemini's free tier
├── common/                     # Protocol, election, heartbeat, discovery, config
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
4. The broker (7001/7002) and LibreTranslate (5001) ports are published on `127.0.0.1` only, because
   the broker protocol has no authentication. Do not expose them.
5. Audio and text are sent to the Gemini API: tell speakers and check Google's current terms for
   your tier. Captions are held in memory only and are not written to disk.

This is still one shared room per dashboard: everyone sees the same captions.

## Evaluating it

`eval/` measures quality on real recordings rather than synthesized speech. Build a manifest
(one JSON object per line: `id`, `lang`, `audio`, `transcript`, optional `english`; FLEURS has this
shape for Burmese, Khmer, Lao and the other languages here), then:

```bash
python -m eval.fleurs_eval asr --manifest data/manifest.jsonl --out results/asr.json
python -m eval.fleurs_eval mt  --manifest data/manifest.jsonl --out results/mt.json --from-asr results/asr.json
python -m eval.failover_trials --trials 30 --out results/failover.json
```

`asr` reports character and word error rate, empty outputs, seconds per clip and real-time factor;
`mt` reports chrF++ against the human English translation. Both give 95% bootstrap intervals, and
a clip that produced nothing scores as a total miss. chrF++ matches sacrebleu to 1e-14.
`failover_trials` kills the leader N times and reports the distribution of time-to-new-leader
(8.12 s, SD 0.03 s over 10 trials with the default 2 s heartbeat / 6 s timeout).

## Testing

```bash
python -m unittest discover -s tests -v
```

or inside Docker: `docker compose --profile test run --rm tests`. The Docker image is stdlib-only, so
tests that need numpy, the Gemini SDK or Node are skipped there (about 55 of 486); run the suite natively
to execute everything.

Browser-side tests run the real `ui/app.js` against a fake DOM with Node's built-in runner
(`node --test tests/js/app.test.js`; they are also part of the Python run when `node` is installed).

| Area | Test files |
|---|---|
| Protocol, config, election, heartbeat, discovery, health check, listener client | `test_protocol.py`, `test_common_modules.py` |
| Broker: registry, handshake, routing, job telemetry, reaper, leadership | `test_broker.py`, `test_broker_server.py`, `test_cluster.py` (real processes) |
| Translator worker: retry rules, Gemini text/Live calls, LibreTranslate, English pivot, worker process | `test_languages.py`, `test_gemini_translation.py`, `test_worker_runtime.py` |
| Speech-to-text: Whisper windows and filters, Gemini transcription, producer, CLI | `test_stt_service.py`, `test_stt_runtime.py`, `test_chunk_pipeline.py` |
| Dashboard server: state, validation, listeners, broker status, HTTP routes, uploads, live slices | `test_web_ui.py`, `test_web_ui_extra.py` |
| Dashboard page and live slicer (JavaScript) | `test_ui_app.py` + `tests/js/`, `test_live_slicer.py` |
| Rate gate, evaluation metrics, evaluation runner, failover trials | `test_gemini_rate_gate.py`, `test_gate_and_eval.py`, `test_evaluation.py`, `test_eval_harness.py` |

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
