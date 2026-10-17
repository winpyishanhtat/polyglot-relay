"""tools/autoscale.py: when a language's worker pool grows or shrinks, and the command that does it."""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import autoscale  # noqa: E402
from autoscale import Autoscaler, Policy  # noqa: E402


def seen(workers, queue=0, waiting=0):
    return {"workers": workers, "queue": queue, "waiting": waiting}


class DecisionTests(unittest.TestCase):
    def scaler(self, **policy):
        return Autoscaler(Policy(**{"minimum": 1, "maximum": 3, "high": 3.0, "cooldown": 60.0, **policy}))

    def test_one_busy_reading_is_a_burst_two_are_a_trend(self):
        scaler = self.scaler()
        self.assertEqual(scaler.decide({"th": seen(1, queue=8)}, 0), {})
        self.assertEqual(scaler.decide({"th": seen(1, queue=8)}, 10), {"th": 2})

    def test_work_with_no_worker_at_all_gets_one_at_once(self):
        scaler = self.scaler(minimum=0)
        self.assertEqual(scaler.decide({"km": seen(0, waiting=1)}, 0), {"km": 1})

    def test_a_pool_below_its_minimum_is_brought_up(self):
        self.assertEqual(self.scaler(minimum=2).decide({"th": seen(1)}, 0), {"th": 2})

    def test_it_never_grows_past_the_maximum(self):
        scaler = self.scaler(maximum=2)
        for t in range(0, 600, 10):
            changes = scaler.decide({"th": seen(2, queue=50)}, t)
            self.assertEqual(changes, {})

    def test_a_pool_shrinks_one_step_after_a_calm_cooldown(self):
        scaler = self.scaler()
        self.assertEqual(scaler.decide({"th": seen(3)}, 0), {})
        self.assertEqual(scaler.decide({"th": seen(3)}, 59), {})
        self.assertEqual(scaler.decide({"th": seen(3)}, 61), {"th": 2})
        self.assertEqual(scaler.decide({"th": seen(2)}, 70), {})          # the next step waits another cooldown
        self.assertEqual(scaler.decide({"th": seen(2)}, 125), {"th": 1})
        self.assertEqual(scaler.decide({"th": seen(1)}, 999), {})         # not below the minimum

    def test_any_backlog_resets_the_calm(self):
        scaler = self.scaler()
        scaler.decide({"th": seen(2)}, 0)
        scaler.decide({"th": seen(2, queue=1)}, 50)
        self.assertEqual(scaler.decide({"th": seen(2)}, 70), {})

    def test_changes_to_one_pool_are_spaced_out_but_pools_are_independent(self):
        scaler = self.scaler()
        scaler.decide({"th": seen(1, queue=9)}, 0)
        self.assertEqual(scaler.decide({"th": seen(1, queue=9), "vi": seen(1, queue=9)}, 5), {"th": 2})
        self.assertEqual(scaler.decide({"th": seen(2, queue=20), "vi": seen(1, queue=9)}, 10), {"vi": 2})
        scaler.decide({"th": seen(2, queue=20)}, 15)
        self.assertEqual(scaler.decide({"th": seen(2, queue=20)}, 20), {})               # still cooling down
        self.assertEqual(scaler.decide({"th": seen(2, queue=20)}, 70), {"th": 3})

    def test_waiting_jobs_count_as_backlog(self):
        scaler = self.scaler()
        scaler.decide({"th": seen(1, waiting=5)}, 0)
        self.assertEqual(scaler.decide({"th": seen(1, waiting=5)}, 5), {"th": 2})


class MetricsParsingTests(unittest.TestCase):
    def test_it_reads_workers_queue_and_waiting_per_language(self):
        text = "\n".join(["# HELP x", 'polyglot_workers{lang="th"} 2', 'polyglot_queue_depth{lang="th"} 7',
                          'polyglot_waiting_jobs{lang="km"} 3', 'polyglot_workers{lang="km"} 0', 'other_metric{lang="th"} 9'])
        self.assertEqual(autoscale.parse_metrics(text), {"th": {"workers": 2, "queue": 7}, "km": {"waiting": 3, "workers": 0}})


class CommandTests(unittest.TestCase):
    def test_the_scale_command_touches_only_that_service(self):
        self.assertEqual(autoscale.scale_command(["compose.yaml", "compose.libretranslate.yaml"], "th", 3),
                         ["docker", "compose", "-f", "compose.yaml", "-f", "compose.libretranslate.yaml", "up", "-d",
                          "--no-deps", "--no-recreate", "--scale", "worker-th=3", "worker-th"])
        self.assertIn("worker-en-1=2", autoscale.scale_command(["compose.yaml"], "en", 2))

    def test_a_dry_run_prints_and_changes_nothing(self):
        with mock.patch.object(autoscale, "read_metrics", return_value={"km": seen(0, waiting=2)}), \
                mock.patch.object(autoscale.subprocess, "run") as run, mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(autoscale.main(["--once", "--dry-run", "--min", "0"]), 0)
        run.assert_not_called()
        self.assertIn("km: 0 -> 1 workers", out.getvalue())

    def test_scaling_runs_docker_compose_and_can_keep_the_shells_key_out(self):
        done = mock.Mock(returncode=0, stderr="")
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "from-the-shell"}), \
                mock.patch.object(autoscale, "read_metrics", return_value={"km": seen(0, waiting=2)}), \
                mock.patch.object(autoscale.subprocess, "run", return_value=done) as run, mock.patch("sys.stdout", new_callable=io.StringIO):
            autoscale.main(["--once", "--min", "0", "--clean-env", "--compose", "compose.yaml"])
        command, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(command[-2:], ["worker-km=1", "worker-km"])
        self.assertNotIn("GEMINI_API_KEY", kwargs["env"])

    def test_an_unreachable_dashboard_is_reported_not_fatal(self):
        with mock.patch.object(autoscale, "read_metrics", side_effect=OSError("refused")), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(autoscale.main(["--once"]), 0)
        self.assertIn("Could not read", out.getvalue())

    def test_bad_limits_are_refused(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            autoscale.main(["--once", "--min", "3", "--max", "2"])


class ScalableWorkerTests(unittest.TestCase):
    def test_auto_advertises_this_machines_address(self):
        from translator_worker import advertised_host
        self.assertEqual(advertised_host("worker-th"), "worker-th")
        with mock.patch("socket.gethostbyname", return_value="172.18.0.7"):
            self.assertEqual(advertised_host("auto"), "172.18.0.7")
        with mock.patch("socket.gethostbyname", side_effect=OSError):
            self.assertEqual(advertised_host("auto"), "127.0.0.1")

    def test_no_compose_file_pins_a_worker_name_that_would_clash_when_scaled(self):
        for name in ("compose.yaml", "compose.real.yaml", "compose.libretranslate.yaml"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn('"--worker-id"', text, name)
            self.assertIn('"--host", "auto"', text, name)


if __name__ == "__main__":
    unittest.main()
