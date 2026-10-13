"""The FLEURS manifest builder, run against a local fake of the Hugging Face layout."""

import gzip
import io
import json
import pathlib
import shutil
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from eval import fleurs_eval, prepare_fleurs

WAV = b"RIFF" + b"\x00" * 60


def tsv(rows):
    return "\n".join("\t".join(r) for r in rows).encode("utf-8")


def archive(members, truncate_to=None):
    """members: (name, bytes, type). Returns .tar.gz bytes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    payload = raw.getvalue()
    return payload[:truncate_to] if truncate_to else payload


class FakeHub:
    def __init__(self, files):
        self.files, self.requests = files, []
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hub.requests.append(self.path)
                body = hub.files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the client stopped reading, which is what an early stop looks like

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/data"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.out = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))
        self.hubs = []

    def hub(self, files):
        hub = FakeHub(files)
        self.hubs.append(hub)
        self.addCleanup(hub.close)
        return hub

    def tiny_hub(self, my_members=None):
        en_rows = [["10", "1.wav", "Hello there.", "hello there", "", "1", "M"],
                   ["11", "2.wav", "Thank you.", "thank you", "", "1", "F"]]
        my_rows = [["10", "100.wav", "MY ONE", "x", "", "1", "M"], ["11", "200.wav", "MY TWO", "x", "", "1", "M"],
                   ["12", "300.wav", "MY THREE", "x", "", "1", "M"]]
        members = my_members if my_members is not None else [
            ("test", b"", "dir"), ("test/100.wav", WAV, "file"), ("test/200.wav", WAV, "file"), ("test/300.wav", WAV, "file")]
        return self.hub({
            "/data/en_us/test.tsv": tsv(en_rows),
            "/data/my_mm/test.tsv": tsv(my_rows),
            "/data/my_mm/audio/test.tar.gz": archive(members),
            "/data/en_us/audio/test.tar.gz": archive([("test/1.wav", WAV, "file"), ("test/2.wav", WAV, "file")]),
        })

    def run_prepare(self, hub, clips=10, languages=None):
        with mock.patch.object(prepare_fleurs.time, "sleep"):   # never really wait between retries
            return self._prepare(hub, clips, languages)

    def _prepare(self, hub, clips, languages):
        return prepare_fleurs.prepare(self.out, clips, languages or {"en_us": "en", "my_mm": "my"}, base_url=hub.url)

    def test_the_manifest_has_one_entry_per_kept_clip_with_relative_audio_paths(self):
        manifest = self.run_prepare(self.tiny_hub())
        mine = [m for m in manifest if m["lang"] == "my"]
        self.assertEqual([m["id"] for m in mine], ["my-100", "my-200", "my-300"])
        self.assertEqual(mine[0]["audio"], "audio/my/100.wav")
        self.assertEqual(mine[0]["transcript"], "MY ONE")
        self.assertTrue((self.out / "audio" / "my" / "100.wav").is_file())

    def test_the_english_reference_is_the_english_transcript_with_the_same_sentence_id(self):
        manifest = {m["id"]: m for m in self.run_prepare(self.tiny_hub())}
        self.assertEqual(manifest["my-100"]["english"], "Hello there.")
        self.assertEqual(manifest["my-200"]["english"], "Thank you.")
        self.assertNotIn("english", manifest["my-300"])        # no English recording of sentence 12

    def test_english_clips_get_no_translation_reference_because_it_would_be_themselves(self):
        manifest = self.run_prepare(self.tiny_hub())
        self.assertTrue(all("english" not in m for m in manifest if m["lang"] == "en"))

    def test_only_the_requested_number_of_clips_is_kept(self):
        manifest = self.run_prepare(self.tiny_hub(), clips=2)
        self.assertEqual(len([m for m in manifest if m["lang"] == "my"]), 2)
        self.assertFalse((self.out / "audio" / "my" / "300.wav").exists())

    def test_the_manifest_file_is_written_and_loads_in_the_evaluation_runner(self):
        self.run_prepare(self.tiny_hub())
        items = fleurs_eval.load_manifest(self.out / "manifest.jsonl", ["my"])
        self.assertEqual(len(items), 3)
        self.assertEqual(pathlib.Path(items[0]["audio"]), (self.out / "audio" / "my" / "100.wav").resolve())

    def test_unsafe_or_unexpected_archive_members_are_never_written(self):
        hostile = [("../evil.wav", WAV, "file"), ("test/../evil2.wav", WAV, "file"), ("/abs/300.wav", WAV, "file"),
                   ("test/notes.txt", b"hi", "file"), ("test/abc.wav", WAV, "file"), ("test/100.wav.exe", WAV, "file"),
                   ("test/nested/200.wav", WAV, "file"), ("test/100.wav", WAV, "file")]
        manifest = self.run_prepare(self.tiny_hub(hostile))
        self.assertEqual([m["id"] for m in manifest if m["lang"] == "my"], ["my-100"])
        written = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())
        self.assertNotIn("evil.wav", [pathlib.PurePosixPath(w).name for w in written])
        self.assertEqual([w for w in written if w.startswith("audio/my/")], ["audio/my/100.wav"])
        self.assertFalse((self.out.parent / "evil.wav").exists())

    def test_oversized_members_are_skipped(self):
        members = [("test/100.wav", WAV * 1000, "file"), ("test/200.wav", WAV, "file")]
        with mock.patch.object(prepare_fleurs, "MAX_WAV_BYTES", 500):
            manifest = self.run_prepare(self.tiny_hub(members))
        self.assertEqual([m["id"] for m in manifest if m["lang"] == "my"], ["my-200"])

    def test_clips_without_a_transcript_row_are_ignored(self):
        members = [("test/999.wav", WAV, "file"), ("test/100.wav", WAV, "file")]
        manifest = self.run_prepare(self.tiny_hub(members))
        self.assertEqual([m["id"] for m in manifest if m["lang"] == "my"], ["my-100"])

    def test_a_repeated_member_is_kept_once(self):
        members = [("test/100.wav", WAV, "file"), ("test/100.wav", WAV, "file"), ("test/200.wav", WAV, "file")]
        manifest = self.run_prepare(self.tiny_hub(members))
        self.assertEqual([m["id"] for m in manifest if m["lang"] == "my"], ["my-100", "my-200"])

    def test_a_download_cut_off_part_way_keeps_the_clips_that_arrived_whole(self):
        padding = bytes(range(256)) * 400    # incompressible-ish so the cut lands inside a later member
        members = [("test/100.wav", WAV + padding, "file"), ("test/200.wav", WAV + padding, "file"),
                   ("test/300.wav", WAV + padding, "file")]
        full = archive(members)
        hub = self.tiny_hub()
        hub.files["/data/my_mm/audio/test.tar.gz"] = full[: len(full) * 2 // 3]
        manifest = self.run_prepare(hub)
        kept = [m["id"] for m in manifest if m["lang"] == "my"]
        self.assertTrue(0 < len(kept) < 3, kept)

    def test_it_stops_reading_the_archive_once_enough_clips_are_kept(self):
        padding = bytes(range(256)) * 4000
        members = [(f"test/{n}00.wav", WAV + padding, "file") for n in range(1, 4)]
        hub = self.tiny_hub(members)
        reads = []
        real_open = prepare_fleurs.fetch

        class Counting:
            def __init__(self, stream):
                self.stream = stream

            def read(self, n=-1):
                data = self.stream.read(n)
                reads.append(len(data))
                return data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

        def opener(url, timeout=60.0):
            stream = real_open(url, timeout)
            return Counting(stream) if url.endswith("my_mm/audio/test.tar.gz") else stream
        prepare_fleurs.prepare(self.out, 1, {"en_us": "en", "my_mm": "my"}, base_url=hub.url, opener=opener)
        self.assertLess(sum(reads), len(hub.files["/data/my_mm/audio/test.tar.gz"]))

    def test_a_transient_failure_fetching_a_transcript_is_retried(self):
        hub = self.tiny_hub()
        attempts = []
        real = prepare_fleurs.fetch

        def flaky(url, timeout=60.0):
            if url.endswith("my_mm/test.tsv") and not attempts:
                attempts.append(url)
                raise TimeoutError("read timed out")
            return real(url, timeout)
        with mock.patch.object(prepare_fleurs.time, "sleep"):
            manifest = prepare_fleurs.prepare(self.out, 10, {"en_us": "en", "my_mm": "my"}, base_url=hub.url, opener=flaky)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len([m for m in manifest if m["lang"] == "my"]), 3)

    def test_giving_up_after_repeated_transcript_failures_raises(self):
        def always_fails(url, timeout=60.0):
            raise TimeoutError("read timed out")
        with mock.patch.object(prepare_fleurs.time, "sleep"), self.assertRaises(TimeoutError):
            prepare_fleurs.prepare(self.out, 10, {"my_mm": "my"}, base_url="http://x/data", opener=always_fails)

    def test_a_dropped_archive_download_is_resumed_for_the_missing_clips(self):
        padding = bytes(range(256)) * 400
        members = [(f"test/{n}00.wav", WAV + padding, "file") for n in (1, 2, 3)]
        full = archive(members)
        hub = self.tiny_hub()
        opens = []
        real = prepare_fleurs.fetch

        def opener(url, timeout=60.0):
            if url.endswith("my_mm/audio/test.tar.gz"):
                opens.append(url)
                if len(opens) == 1:   # first connection dies two thirds of the way through
                    hub.files["/data/my_mm/audio/test.tar.gz"] = full[: len(full) * 2 // 3]
                else:
                    hub.files["/data/my_mm/audio/test.tar.gz"] = full
            return real(url, timeout)
        with mock.patch.object(prepare_fleurs.time, "sleep"):
            manifest = prepare_fleurs.prepare(self.out, 3, {"en_us": "en", "my_mm": "my"}, base_url=hub.url, opener=opener)
        self.assertEqual(sorted(m["id"] for m in manifest if m["lang"] == "my"), ["my-100", "my-200", "my-300"])
        self.assertEqual(len(opens), 2)

    def test_tsv_parsing_tolerates_quotes_and_short_lines(self):
        rows = prepare_fleurs.read_tsv('1\ta.wav\tHe said "hi"\tnorm\n2\tshort\n3\tb.wav\t\'x\'\tn\n')
        self.assertEqual([r[0] for r in rows], ["1", "3"])
        self.assertEqual(rows[0][2], 'He said "hi"')

    def test_language_codes_map_to_the_projects_codes(self):
        self.assertEqual(prepare_fleurs.LANGUAGES["fil_ph"], "tl")
        self.assertEqual(set(prepare_fleurs.LANGUAGES.values()), {"en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"})

    def test_a_missing_file_on_the_hub_is_an_error_not_an_empty_manifest(self):
        hub = self.tiny_hub()
        del hub.files["/data/my_mm/audio/test.tar.gz"]
        with self.assertRaises(Exception):
            self.run_prepare(hub)

    def test_command_line(self):
        with mock.patch.object(prepare_fleurs, "prepare", return_value=[{"id": "x"}]) as prepare, \
                mock.patch("builtins.print"):
            code = prepare_fleurs.main(["--out", str(self.out), "--clips", "7", "--langs", "my", "km"])
        self.assertEqual(code, 0)
        self.assertEqual(prepare.call_args.args[1], 7)
        self.assertEqual(prepare.call_args.args[2], {"my_mm": "my", "km_kh": "km"})


if __name__ == "__main__":
    unittest.main()
