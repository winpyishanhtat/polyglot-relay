# Chunk translation and distributed processing

This console supports English, Burmese, Indonesian, Malay, Thai, Vietnamese,
Filipino, Khmer and Lao. Select the actual spoken language before submitting.
The source channel displays the transcript; the other eight pools translate it.
Outputs are text captions, not dubbed audio or a newly rendered video.

## Run on Windows with Gemini's free tier

Start Docker Desktop. In the root `.env`, keep your private key and add:

```dotenv
GEMINI_API_KEY=your_private_key
GEMINI_TIER=free
GEMINI_RATE_LIMIT_PER_MINUTE=5
POLYGLOT_FILE_CHUNK_SECONDS=15
```

`5` is a conservative local budget, not a universal Google quota. Check your
selected model's active limits in Google AI Studio and adjust it accordingly.
`compose.real.yaml` now defaults to `free`; it uses the code's free-tier model
unless `GEMINI_TEXT_MODEL` is explicitly set. Set that variable to an available
text model in your project if the default model is unavailable.

```powershell
cd D:\Projects\polyglot-relay
docker compose -f compose.yaml -f compose.real.yaml up --build -d
```

Open http://localhost:8000. Select your source language, then type text, upload
audio/video (maximum 25 MB), or record and press Stop. A rebuild is required
after these code changes. Browser recording is uploaded after Stop, then
processed in chunks; it does not send live audio while you are speaking.

For an API-free distributed demonstration:

```powershell
docker compose up --build -d
```

Or use the isolated native preview, without Docker or translation dependencies:

```powershell
python tools/preview_cluster.py
```

The preview uses http://localhost:18080 and broker ports 17601/17602. Ctrl+C
stops its child processes. Audio still requires Whisper/ffmpeg; typed input
does not. Mock outputs are marked as mock, not presented as real translations.

## How chunks move

1. The server accepts an upload and returns immediately; transcription runs
   in the background. Only one upload is transcribed at a time.
2. Whisper decodes the file's audio, then transcribes 15-second windows in
   order. Silence is skipped. Each speech window produces one text chunk.
   Decoding still loads audio into memory; inference, rather than decoding,
   is incremental. Windows can split sentences and lose context at boundaries.
3. As soon as window 1 is transcribed, its text is sent to the elected leader.
   Whisper can work on window 2 while workers translate window 1. The server
   does not collect the whole file's transcript before starting translation.
4. The leader publishes the source unchanged and sends the text to one worker
   in each of the other eight pools. A per-language round-robin cursor selects
   workers. English has two workers by default, so choose a non-English source
   to demonstrate English translation load balancing.
5. Every worker has a FIFO translation queue and a separate socket-reading
   loop. Waiting for Gemini does not prevent heartbeats or result delivery.
6. Before every Gemini text request, including retries, free-tier workers
   reserve a slot in one shared FIFO rate gate. An unavailable gate fails the
   request rather than silently bypassing the configured budget. The gate
   limits request count, not tokens or daily usage.
7. Results return through the leader and are broadcast to the language's pool
   listeners. Numbered captions are displayed in source order, even when
   results arrive out of order. The progress table shows the latest 40 chunks;
   the server retains 200 chunks and 50 captions per language (12 shown per
   card). These are bounded in-memory histories, not permanent storage.

At the illustrative budget of five requests/minute, one speech chunk needs
eight requests: at least some languages wait into the next quota window. A
one-minute recording with four speech windows needs about 32 initial requests,
or roughly 6.4 minutes of sustained request capacity at that budget, plus
transcription, API latency and retries. Initial bursts affect exact elapsed time.
Eight concurrent language pools do not imply eight simultaneous API responses.
Longer windows (up to 60 seconds) trade slower first results for fewer requests.

## Watching distributed behavior

- **Broker replicas:** live leader/standby/offline roles, with the leader's
  registered-worker count. A standby is an election peer, not a durable copy
  of a replicated message log.
- **Translation pools:** every registered worker, heartbeat age, queued and
  active counts. Counts refer to retained broker history.
- **Chunk journey:** source, sending, queued, processing, quota wait,
  translating, retrying, done, failed, unavailable and interrupted states.
  Hover a status for the assigned worker and further detail.
- **Captions:** numbered outputs with backend labels. Mock mode is explicitly
  distinguished from real Gemini translation.

Test broker failover (broker 2 is normally the leader):

```powershell
docker compose -f compose.yaml -f compose.real.yaml stop broker-2
# Watch broker 1 become leader, then submit a new text chunk.
docker compose -f compose.yaml -f compose.real.yaml start broker-2
```

To test worker failover, select a non-English source and stop `worker-en-1`.
The surviving English worker handles new chunks. Restart the stopped worker to
watch registration. Do not claim exactly-once delivery: an in-flight chunk can
be lost during a broker/worker failure. Such work is marked interrupted when
detected; resubmit the affected text. The original protocol has no durable
queue, acknowledgement or automatic replay. Queues reject new work at the
configured implementation bound (100 active/pending dashboard chunks and
100 queued chunks per worker) rather than accumulating indefinitely.

## API key, fallback and billing

No billing key is needed for an offline demo or short free-tier translations
within your actual quota. Sustained near-real-time eight-language translation
will generally need greater quota; billing may help, but does not guarantee
latency, accuracy or unlimited requests. Verify model and project limits first.

Google applies rate limits per project, not per API key. A second key from the
same project does not increase quota. No automatic key rotation was added.
Daily quota exhaustion is reported as failure without repeatedly retrying it.
The existing optional LibreTranslate backend is a separate alternative, with
language coverage dependent on installed models. The pure Gemini overlay is
the configured path for this workflow.

Official quota reference: https://ai.google.dev/gemini-api/docs/rate-limits

Never put keys in the UI or commit `.env`. Native scripts do not load `.env`
automatically; export their environment variables in PowerShell when running
them manually. Docker Compose reads the root `.env`.

## Validation

```powershell
python -m unittest discover -s tests -v
node --check ui/app.js
```

The suite includes incremental transcription, incremental upload dispatch,
eight-target routing, quota gate failure behavior, failure telemetry and a
multi-process dashboard test with actual mock worker sockets. Mock tests do
not certify Google API availability or real speech/translation accuracy.
