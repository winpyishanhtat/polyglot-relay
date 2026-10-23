# Presentation demo (about 12 minutes)

A script for showing Polyglot Relay live: what to run, what to put on screen, and what to say. Everything runs
on one laptop with Docker; phones on the same Wi-Fi can open the listener page.

## Before the presentation (15 minutes ahead)

```bash
# the real stack: LibreTranslate + Gemini, history on disk, monitoring
docker compose -f compose.yaml -f compose.libretranslate.yaml -f compose.monitoring.yaml up -d
python tools/smoke_test.py            # must print "9 of 9 checks passed"
```

Open four browser windows:

| Window | Address | Shows |
|---|---|---|
| A - presenter | http://localhost:8000/?room=demo | the dashboard: record button, network pulse, chunk table |
| B - audience | http://localhost:8000/listen?room=demo&langs=my,th,vi,km | every sentence in four languages at once |
| C - monitoring | http://localhost:3000 | Grafana, "Polyglot Relay" dashboard, last 15 minutes |
| D - terminal | this folder | for the failure commands below |

For phones: find the laptop's address (`ipconfig`, e.g. 192.168.1.20) and show a QR code for
`http://192.168.1.20:8000/listen?room=demo`. (The microphone works only on localhost or HTTPS, so the presenter
speaks into the laptop; phones only listen.)

## 1. One voice, every language (3 min)

1. In window A, choose **Speaking in: English**, tick **Live captions**, press **Record** and say two or three
   sentences with a name in them ("Welcome to UIT. Today Win Pyi Shan Htat presents Polyglot Relay.").
2. Point at window B: each sentence appears once with Burmese, Thai, Vietnamese and Khmer underneath, filling
   in as each language arrives. Switch on **Read aloud** and choose Thai.
3. Say: "The browser does not ask for updates; the server pushes each translation the moment a worker
   finishes it. The glossary keeps 'UIT' and the names unchanged in every language."
4. Stop recording. Speak Burmese or Khmer if a team member can: those go through Gemini, the others through
   the free self-hosted LibreTranslate.

## 2. Rooms (1 min)

Open `http://localhost:8000/?room=hall-b` in a new tab and type a sentence. It appears only in hall-b; window
B (room demo) does not show it. "One relay, many audiences; the brokers and workers do not even know rooms
exist - the room travels in the chunk id."

## 3. Kill the leader while people are listening (3 min)

In window C, point at **Leaders (must be 1)** and **Leader broker**. Start a steady stream of captions:

```bash
python tools/load_test.py --count 90 --rate 1 --rooms 1 --wait 60
```

While it runs, kill the leading broker (the Grafana panel shows which; normally 3):

```bash
docker kill polyglot-relay-broker-3-1
```

Watch in Grafana: "Broker state" shows broker 3 down and broker 2 taking over within about 5 seconds;
"Leaders" stays at 1. Then kill a Thai worker:

```bash
docker kill polyglot-relay-worker-th-1
```

"Redeliveries" rises: jobs the worker was holding go to another worker or wait for one. Start both again:

```bash
docker start polyglot-relay-broker-3-1 polyglot-relay-worker-th-1
```

When the load test finishes it prints that every caption arrived in every language. Say: "At-least-once
delivery: a job is kept until a worker answers, mirrored to the standby brokers, and written to disk, so even
restarting every broker at once loses nothing."

## 4. The numbers (2 min)

Show the README or report tables:

- failover: about 4.6 s without a leader in the chaos test, never two leaders (Table 8.6);
- the chaos test itself: `python tools/chaos_test.py --duration 60 --every 10` ends with
  "RESULT: PASSED - every caption arrived in every language and there was never more than one leader";
- speech accuracy on FLEURS (fleurs_150_summary.md) and the fragment guard: 0 of 3,597 real sentences dropped;
- 1,000+ automated tests and a CI pipeline that runs the smoke, load and chaos tests on every push.

## 5. Raft and security, if there is time (2 min)

"Bully keeps even a lone broker working, but a network split could give two leaders. With
`POLYGLOT_ELECTION=raft` a broker needs a majority of votes, so that cannot happen: with two of three brokers
killed, there is no leader rather than a wrong one." With `POLYGLOT_CLUSTER_SECRET` set, a process that does
not know the secret cannot join: show

```bash
python -c "import socket,json; s=socket.create_connection(('127.0.0.1',7003)); s.sendall(b'{\"type\":\"register\",\"role\":\"worker\",\"lang\":\"th\",\"worker_id\":\"intruder\",\"host\":\"x\",\"port\":1}\n'); print(s.recv(200))"
```

which prints `authentication failed`.

## If something goes wrong

- A language card stays empty: `docker compose ps` - restart the worker (`docker start polyglot-relay-worker-xx-1`).
- Gemini languages (Burmese, Khmer, Lao) are slow or fail: the key's quota; type in English instead.
- Grafana shows "No data": wait 10 seconds after starting; Prometheus scrapes every 5 s.
- Nothing works: `python tools/smoke_test.py` says which part is missing.
