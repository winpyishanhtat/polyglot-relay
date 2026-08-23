# Codex Implementation Prompt — Polyglot Relay (Distributed Architecture)

Use this prompt to drive implementation in Codex (or a similar AI coding tool). Work through the modules **in order** — each one should be working and testable before moving to the next. Ask for clarification if a requirement is ambiguous rather than guessing.

---

## Project context

Build a distributed, real-time multilingual speech captioning system in Python 3.10+. A speaker's live microphone audio is transcribed via self-hosted Whisper and sent to a **replicated broker** (2 instances, one elected leader) which fans transcript chunks out to **dynamically registered, load-balanced pools of translator workers** — one pool per language (English, Japanese, Burmese). The system must tolerate both a translator worker crashing and the leader broker itself crashing, without losing the pipeline.

Full design context is in the accompanying `README.md` — read it first for architecture, folder structure, and scope boundaries. Do not use an external message broker library (Redis, Kafka, ZooKeeper, etc.) or an off-the-shelf consensus library — the broker, election, and registration logic must be built from raw Python sockets, since implementing these mechanisms yourself is the point of the assignment.

---

## Module 1 — Shared protocol and config

**Goal**: define the message format and shared constants every component will use.

- `common/config.py`: broker replica list (`[{"id": 1, "host": ..., "port": ...}, {"id": 2, ...}]`), target languages (`["en", "ja", "my"]`), audio chunk duration (2.5s default), heartbeat interval (2s), heartbeat timeout (6s), election timeout.
- `common/protocol.py`: newline-delimited JSON messages. Define message types:
  - `register` (`role`: "worker"|"stt", `lang` if worker, `worker_id`, `host`, `port`)
  - `transcript_chunk` (`chunk_id`, `text`, `timestamp`)
  - `translated_chunk` (`chunk_id`, `lang`, `text`, `timestamp`)
  - `heartbeat` (`sender_id`, `role`: "broker"|"worker")
  - `election` (`candidate_id`)
  - `election_ok` (`responder_id`)
  - `leader_announce` (`leader_id`)
  Provide `encode(msg: dict) -> bytes` and `decode_stream(sock) -> Iterator[dict]`.

**Acceptance check**: round-trip test for each message type through a loopback socket pair.

---

## Module 2 — Bully leader election between broker replicas

**Goal**: `common/election.py` — implement the Bully algorithm, used by `broker.py` instances to elect a leader among themselves.

Requirements:
- Each broker replica has a fixed numeric ID (from config) and knows the addresses of all other replicas.
- On startup, or when a replica stops receiving heartbeats from the current leader, it starts an election: sends `election` to all replicas with a higher ID.
  - If none respond with `election_ok` within a timeout, it declares itself leader and sends `leader_announce` to all replicas.
  - If a higher-ID replica responds, it waits for that replica to announce itself as leader (with its own timeout, restarting election if no announcement comes).
- Only the leader accepts connections from the STT service and translator workers; standby replicas should refuse or redirect such connections.
- Standby replicas send periodic heartbeats to the leader and detect its failure via missed heartbeats, triggering a new election.

**Acceptance check**: start 2 broker replica processes, confirm exactly one declares itself leader via logs. Kill the leader process, confirm the standby detects this within the configured timeout and becomes the new leader, logging the transition clearly.

---

## Module 3 — Broker: registration, fan-out, and load balancing

**Goal**: extend `broker.py` (only meaningful on whichever replica is currently leader) to:
- Accept `register` messages from translator workers, storing them in an in-memory pool keyed by `lang` (a list of `(worker_id, host, port)` per language).
- Accept a `register` from the STT service similarly.
- On receiving a `transcript_chunk` from the STT service, forward it to **one** worker per language pool using round-robin selection within that pool (so with 2 English workers, alternate between them; with 1 Japanese worker, it always goes to that one).
- Handle a worker disconnecting cleanly: remove it from its pool without affecting other workers or other languages.
- Log every registration, every chunk forwarded (to which worker), and every deregistration.

**Acceptance check**: register 2 fake English worker sockets and 1 fake Japanese worker socket, send 4 fake transcript chunks, and assert the 2 English workers each received 2 chunks (round-robin) while the Japanese worker received all 4.

---

## Module 4 — Speech-to-text service with leader discovery

**Goal**: `stt_service.py` — captures live microphone audio, buffers into ~2.5s chunks, runs each through a locally-loaded Whisper model, and sends transcripts to the **current leader broker**.

Requirements:
- On startup and on any connection failure, attempt to connect to each broker replica in `config.py` in turn until one accepts the connection as leader (a non-leader replica should reject/redirect, per Module 2).
- Handle empty transcripts for silent chunks by not sending them.
- Support a `--input-file` flag to feed a WAV file instead of live mic input, for testing without a microphone.
- Print each transcript to console as produced.

**Acceptance check**: run with `--input-file` against a short test WAV, confirm reasonable transcripts print and are sent to whichever broker is currently leader (verify via broker's registration log).

---

## Module 5 — Translator worker with dynamic registration

**Goal**: `translator_worker.py --lang <en|ja|my>` — on startup, discovers and connects to the current leader broker the same way the STT service does, sends a `register` message, then:
- Receives `transcript_chunk` messages forwarded to it and translates through Gemini: PCM-bearing speech chunks use `gemini-3.5-live-translate-preview`, while text-only chunks use `gemini-3.6-flash`. It serves the result to connected listener clients as `translated_chunk`.
- Sends periodic heartbeats to the broker.
- Handles translation API errors by logging and skipping the chunk rather than crashing.
- Supports running multiple instances for the same `--lang` simultaneously (each gets a distinct `worker_id`, e.g. a UUID or PID-based ID) — this is what makes the load-balanced pool possible, so make sure nothing assumes there's only one worker per language.

**Acceptance check**: start 2 instances with `--lang en` and 1 with `--lang ja`, send several fake transcript chunks through the broker, and confirm (via logs) that English chunks alternate between the two EN instances while Japanese chunks all go to the single JA instance.

---

## Module 6 — Listener client

**Goal**: `listener_client.py --lang <en|ja|my>` — connects to an assigned worker instance for its language (query the broker for a currently-registered instance of that language, then connect directly) and prints incoming captions with a timestamp.

Keep this simple for MVP: terminal output is fine. If the connected worker instance disconnects mid-session (e.g. it crashed), the listener should attempt to reconnect to a different registered instance of the same language rather than just hanging — query the broker again for a live instance.

---

## Module 7 — Failure and recovery scenarios (integration testing)

Write a short manual test script / checklist (can be a markdown checklist in `eval/failure_scenarios.md`, doesn't need to be automated) covering:

1. **Leader broker failure**: kill the leader mid-session. Confirm standby becomes leader within timeout, and STT service + all workers reconnect to it without manual intervention.
2. **Single-instance worker failure**: with 1 worker for Japanese, kill it. Confirm English/Burmese are unaffected, and Japanese captions stop until the worker restarts and re-registers.
3. **Pooled worker failure**: with 2 workers for English, kill one. Confirm English captions continue uninterrupted via the surviving instance, and load balancing adjusts to send all chunks to the survivor.
4. **New worker joining mid-session**: start a second English worker while the system is running. Confirm it registers and starts receiving a share of chunks without restarting anything else.

Log timing for each scenario (detection time, recovery time) — this becomes your evaluation data.

---

## Module 8 — Evaluation harness

**Goal**: `eval/evaluate.py` — computes BLEU score per language against `eval/benchmark_sentences.json`, WER for transcription, and parses the Module 7 scenario logs to report election/recovery timing. Write results to `eval/results.json`.

---

## General implementation notes

- Prefer clear, timestamped logging over silent failure at every stage — the grading value here is largely about demonstrating the distributed mechanisms working correctly under failure, not just a working audio pipeline.
- Keep each module runnable and testable in isolation (with fake/mock sockets where needed) before wiring the full system together.
- If Whisper performance on the development machine is too slow for real ~2.5s chunk turnaround, fall back to the `tiny` model and note the tradeoff in the README rather than silently degrading the demo.
- Do not silently deviate from the Bully algorithm, round-robin balancing, or registration design specified here and in the README — ask before substituting a different mechanism (e.g. don't quietly switch to a simpler hardcoded primary/backup without flagging it, since the whole point of this version is demonstrating real election and registration logic).
