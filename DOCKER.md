# Docker run and manual test guide

The default Compose stack uses mock translation and text input. It exercises
leader election, registration, routing, reconnects, listener delivery, and
failover without downloading Whisper or requiring Google credentials.

## One-command demo

Run the entire project with one command:

```powershell
docker compose up --build
```

This starts two brokers, ten translator workers (2 for English, 1 each
for the other 8 ASEAN languages), and the web dashboard (which maintains its own
live listener connection for every language).
Open [http://localhost:8000](http://localhost:8000) to use the visual control
room. From there you can upload an audio/video file or record live through
your microphone (transcribed with real Whisper even in this mock-translation
stack — only the translation step is mocked), run the four-caption demo,
browse the 9 live caption feeds, and monitor broker and worker health. Press
`Ctrl+C` to stop the complete stack. Compose stops the containers together.

## 1. Prerequisites

Install Docker Desktop, select Linux containers, and start Docker Desktop. From
PowerShell, change to the project directory and verify Docker:

```powershell
cd "D:\Real Time Multilanguage Translator"
docker version
docker compose version
```

## 2. Run the tests in Docker

```powershell
docker compose --profile test build tests
docker compose --profile test run --rm tests
```

The expected result is `Ran 486 tests` followed by `OK (skipped=55)`. The
lightweight mock image has no numpy, Gemini SDK or Node, so the tests that need
them are skipped there; run `python -m unittest discover -s tests` natively to run all of them.

## 3. Start the complete mock cluster

```powershell
docker compose --profile console up --build -d
docker compose ps
```

The `console` profile adds the three terminal listeners used by the command-line
tests below. The dashboard remains available at `http://localhost:8000`. Wait
until both brokers and the UI show `healthy`. The automatic console demo is
optional and can be enabled with `docker compose --profile demo up --build`.

Inspect election and registrations:

```powershell
docker compose logs broker-1 broker-2
docker compose logs worker-en-1 worker-en-2 worker-th worker-my
```

Broker 2 normally becomes leader because it has the higher Bully ID.

## 4. Send captions through the cluster

The `stt` tool container reads one transcript chunk per input line:

```powershell
@(
  "Good morning everyone"
  "Welcome to the multilingual meeting"
  "Please remain calm"
  "The meeting starts now"
) | docker compose --profile tools run --rm -T stt
```

Read the listener output:

```powershell
docker compose logs --since 2m listener-en listener-my
```

English displays the original text. Every other mock worker prefixes its
output with its language code, e.g. `[my]`, `[th]`, `[id]`; this
deliberately tests routing rather than translation quality. The same pattern
holds for all 9 registered languages (`common/config.py`'s `LANGUAGES`
dict) — the dashboard at http://localhost:8000 shows all of them at once.

## 5. Verify English round-robin routing

```powershell
docker compose logs --since 2m broker-1 broker-2 broker-3 | Select-String "Forwarded chunk.*to en worker"
```

The chunks should alternate between the two English workers (each picks its own id, such as `en-3f9a1c2b`;
the leader gives each job to the one with the fewest unfinished jobs). Any pool can be scaled the same way:
`docker compose up -d --scale worker-th=3`, or automatically with `tools/autoscale.py` (see README).

## 6. Test a pooled worker failure

Stop one English worker:

```powershell
docker compose stop worker-en-1
"Caption after worker failure" | docker compose --profile tools run --rm -T stt
docker compose logs --since 1m broker-1 broker-2 worker-en-2 listener-en
```

The surviving worker should receive the caption and the English listener should
continue. Rejoin the stopped worker dynamically:

```powershell
docker compose start worker-en-1
docker compose logs --since 30s worker-en-1 broker-1 broker-2
```

## 7. Test a single-language failure

```powershell
docker compose stop worker-th
"Thai worker is unavailable" | docker compose --profile tools run --rm -T stt
docker compose logs --since 1m broker-1 broker-2 listener-en listener-my
```

English and Burmese continue while Thai pauses. Restore it with:

```powershell
docker compose start worker-th
```

## 8. Test leader failover

Broker 2 is normally leader. Stop it and wait beyond the six-second heartbeat
timeout:

```powershell
docker compose stop broker-2
Start-Sleep -Seconds 8
docker compose logs --since 30s broker-1
```

Broker 1 should log `Leader heartbeat timeout` and `elected leader`. Confirm the
workers reconnect and send another caption:

```powershell
docker compose logs --since 30s worker-en-1 worker-en-2 worker-th worker-my
"Caption after broker failover" | docker compose --profile tools run --rm -T stt
docker compose logs --since 1m listener-en listener-my
```

Bring the higher-ID broker back:

```powershell
docker compose start broker-2
Start-Sleep -Seconds 4
docker compose logs --since 30s broker-1 broker-2
```

## 9. Run the evaluation tool in Docker

```powershell
docker compose --profile test run --rm tests python eval/evaluate.py
```

Because the container is disposable, the generated `eval/results.json` remains
inside that container. Run the same command locally if you want the file written
to the host workspace.

## 10. Shut everything down

```powershell
docker compose down --remove-orphans
```

## Real Gemini translation mode

Translator workers use a separate, lightweight `translate` image (just the
`google-genai` SDK — no Whisper/PyTorch/ffmpeg), so all 9 language workers
still build and start quickly.

**For real translation of dashboard uploads/recordings** (the common case —
no separately-supplied audio file needed, since the dashboard transcribes
whatever you upload or record itself and talks straight to the broker
network from there):

```powershell
$env:GEMINI_API_KEY = "your-google-ai-studio-api-key"
docker compose -f compose.yaml -f compose.real.yaml up --build -d
```

Open http://localhost:8000, upload an MP3/audio/video file or record live
through your microphone, and it now translates via the real Gemini API for
all 9 languages.

`compose.real.yaml` assumes `GEMINI_API_KEY` belongs to a **billing-enabled
(paid)** Google Cloud project by default (`GEMINI_TIER=paid`): it skips the
free-tier per-worker stagger delay and uses the higher-quality
`gemini-pro-latest` text model. If your key is still on the free tier, set
`GEMINI_TIER=free` first to restore the staggered, `gemini-3.6-flash`
defaults (see "Rate limits" below for why that matters):

```powershell
$env:GEMINI_API_KEY = "your-google-ai-studio-api-key"
$env:GEMINI_TIER = "free"
docker compose -f compose.yaml -f compose.real.yaml up --build -d
```

**If you'd rather feed a WAV file through the `stt` CLI tool** instead of the
dashboard's own upload button, add the third, optional layer (this is the
only thing that needs `AUDIO_FILE` — it's kept in its own file so it doesn't
force everyone else to set it):

```powershell
$env:GEMINI_API_KEY = "your-google-ai-studio-api-key"
$env:AUDIO_FILE = "C:\path\to\speech.wav"
docker compose -f compose.yaml -f compose.real.yaml -f compose.audio.yaml --profile tools up --build -d
docker compose -f compose.yaml -f compose.real.yaml -f compose.audio.yaml --profile tools run --rm stt
docker compose -f compose.yaml -f compose.real.yaml -f compose.audio.yaml logs --since 5m listener-en listener-my
```

By default the `stt` service sends only text, so every worker translates via
the fast Gemini text endpoint (`gemini-pro-latest` on the default `paid` tier,
`gemini-3.6-flash` on `free`) — this is the reliable path and what you want
for a demo across many languages. To instead exercise Gemini Live
speech-to-speech translation for a specific run, add `--send-audio` to the
`stt` service's command in `compose.audio.yaml`; expect it to be slower and,
for some of the newer ASEAN languages, not yet supported by the Live API.

Clean up the real stack with every Compose file you used, so the
configuration matches:

```powershell
docker compose -f compose.yaml -f compose.real.yaml -f compose.audio.yaml down --remove-orphans
```

Google's Gemini free tier enforces a low **daily** request quota per project
(not just per-minute) — a single caption fans out to every language pool at
once, so it's easy to exhaust in one or two submissions. If a worker's logs
show `RESOURCE_EXHAUSTED` with `GenerateRequestsPerDayPerProjectPerModel` in
the message, that's a hard cap that will not recover for hours no matter how
much you retry — see the free translation mode below instead, or enable
billing on the Google Cloud project behind your API key (and set
`GEMINI_TIER=paid`, the compose default, so the workers stop staggering
requests they no longer need to).

## Free translation mode (LibreTranslate, self-hosted, no API key)

This runs an open-source translation engine in your own container instead
of calling a metered third-party API — no quota, no billing, no account:

```powershell
docker compose -f compose.yaml -f compose.libretranslate.yaml up --build -d
```

First boot downloads language models and can take a few minutes — watch
progress with:

```powershell
docker compose -f compose.yaml -f compose.libretranslate.yaml logs -f libretranslate
```

**Confirmed gap**: Argos Translate (the engine behind LibreTranslate) does
not have models for Burmese (`my`), Khmer (`km`), or Lao (`lo`) — the
compose file's `LT_LOAD_ONLY` deliberately excludes them, since including
even one unavailable code makes LibreTranslate 1.9.6 fail to load its
*entire* language set instead of just skipping the bad one, crash-looping
the container. Those three always need the Gemini fallback below; there is
no free/self-hosted path for them here. The other 6 (English, Indonesian,
Malay, Thai, Vietnamese, Filipino) should load fine — confirm with:

```powershell
curl http://localhost:5001/languages
```

Each worker discovers this itself at runtime (no hardcoded language list in
the worker code), and automatically falls back to Gemini for `my`/`km`/`lo`
— **if** you also export `GEMINI_API_KEY` before the command above. Without
it, those three simply show no captions rather than erroring, which is fine
for a fully free setup covering the other 6. Force one backend or the
other explicitly with `$env:TRANSLATION_BACKEND = "libretranslate"` (free
only, errors instead of silently falling back) or `"gemini"` (skip
LibreTranslate entirely).

For Docker Desktop on Windows, audio-file input is considerably more reliable
than passing a live microphone into a Linux container. Use the non-Docker
`stt_service.py` command when live microphone capture is required.
