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
├── eval/                       # WER/BLEU evaluation harness
├── tests/                      # 65-test unittest suite
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

## Testing

```bash
python -m unittest discover -s tests -v
```

or inside Docker: `docker compose --profile test run --rm tests`.

---

For the deep dive — component-by-component design rationale, rate-limit/quota troubleshooting, design tradeoffs, and the distributed-systems concept mapping for your report — see **[docs/DETAILS.md](docs/DETAILS.md)**.
