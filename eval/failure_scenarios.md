# Failure and recovery checklist

Use `--mock` workers and `stt_service.py --stdin` to test without cloud credentials or a microphone.

- [ ] **Leader failure:** terminate the current leader; confirm the standby takes over and clients reconnect. Record failure and recovery timestamps.
- [ ] **Single-language worker failure:** terminate the sole TH worker. Confirm EN/MY continue and TH resumes after restart.
- [ ] **Pooled English worker failure:** terminate one of two EN workers and confirm the survivor receives subsequent chunks.
- [ ] **Worker joins live:** start a second EN worker and confirm forwarding alternates between both IDs.

Scenario logs may contain JSON lines like `{"scenario":"leader_failure","event":"failure","timestamp":1.0}`.
Run `python eval/evaluate.py --scenario-log eval/scenario_events.jsonl` to include timings in the results.
