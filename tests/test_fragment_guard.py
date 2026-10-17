"""The spurious-fragment guard: transcripts invented for silence or music are dropped, real speech never is."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import fragment_guard
import stt_service
from eval import fragment_guard_eval
from fragment_guard import is_spurious, normalise, reason

LOUD, QUIET = 0.2, 0.001


class NormaliseTests(unittest.TestCase):
    def test_case_punctuation_and_symbols_go_letters_and_marks_stay(self):
        self.assertEqual(normalise("  Thanks for WATCHING!!! ♪ "), "thanks for watching")
        self.assertEqual(normalise("Subtitles by the Amara.org community"), "subtitles by the amara org community")
        self.assertEqual(normalise("ขอบคุณครับ"), "ขอบคุณครับ")  # Thai marks kept


class StockPhraseTests(unittest.TestCase):
    def test_stock_phrases_are_dropped_whatever_the_audio(self):
        for text in ("Thanks for watching!", "THANK YOU FOR WATCHING.", "Subtitles by the Amara.org community",
                     "[Music]", "(upbeat music)", "Please subscribe to my channel!",
                     "Hãy subscribe cho kênh Ghiền Mì Gõ Để không bỏ lỡ những video hấp dẫn",
                     "Terima kasih telah menonton.", "ご視聴ありがとうございました"):
            self.assertTrue(is_spurious(text, LOUD), text)
            self.assertTrue(is_spurious(text, None), text)

    def test_a_sentence_that_merely_contains_a_stock_phrase_is_kept(self):
        for text in ("Thanks for watching the demo, now let's look at the results.",
                     "The music festival starts on Friday.", "Please subscribe to the newsletter for updates on the project."):
            self.assertFalse(is_spurious(text, QUIET), text)

    def test_courtesies_are_dropped_only_from_quiet_audio(self):
        for text in ("Thank you.", "Bye.", "Okay.", "you", "ขอบคุณครับ"):
            self.assertTrue(is_spurious(text, QUIET), text)
            self.assertFalse(is_spurious(text, LOUD), text)
            self.assertFalse(is_spurious(text, None), text)          # loudness unknown: keep it

    def test_symbols_only(self):
        for text in ("...", "♪", "♪♪♪", "- -", "?!"):
            self.assertTrue(is_spurious(text, LOUD), text)
        self.assertFalse(is_spurious("", LOUD))
        self.assertFalse(is_spurious("   ", LOUD))


class RepetitionTests(unittest.TestCase):
    def test_a_word_or_syllable_looping_is_dropped(self):
        for text in ("the the the the the the the", "so so so so so so", "ha ha ha ha ha ha", "ა" * 14, "lalalalalalala"):
            self.assertTrue(is_spurious(text, LOUD), text)

    def test_ordinary_repetition_in_real_speech_is_kept(self):
        for text in ("No, no, no, that is not what I said.", "Very very good.", "We need more, more and more data to decide.",
                     "1000000 people", "Bye bye, see you tomorrow at the meeting."):
            self.assertFalse(is_spurious(text, LOUD), text)


class SwitchTests(unittest.TestCase):
    def test_it_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_FRAGMENT_GUARD": "0"}):
            self.assertFalse(is_spurious("Thanks for watching!", QUIET))
        self.assertIsNotNone(reason("Thanks for watching!"))


class TranscriberWiringTests(unittest.TestCase):
    def test_the_transcriber_drops_a_fragment_and_logs_why(self):
        with self.assertLogs("polyglot.stt", level="INFO") as logs:
            self.assertTrue(stt_service._spurious("Thanks for watching!", None))
        self.assertTrue(any("not speech" in line for line in logs.output))
        self.assertFalse(stt_service._spurious("The meeting starts at nine.", None))

    def test_loudness_of_something_that_is_not_audio_is_unknown(self):
        self.assertIsNone(stt_service._loudness(None))


class EvaluationToolTests(unittest.TestCase):
    def test_it_reports_real_speech_dropped_and_fragments_caught(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = Path(directory.name)
        (data / "results").mkdir()
        with open(data / "manifest.jsonl", "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "en-1", "lang": "en", "transcript": "However, the meeting went well."}) + "\n")
            handle.write(json.dumps({"id": "th-1", "lang": "th", "transcript": "สวัสดี"}) + "\n")
        (data / "results" / "asr_gemini_en.json").write_text(json.dumps({"results": [
            {"id": "en-1", "lang": "en", "hypothesis": "However the meeting went well."},
            {"id": "en-2", "lang": "en", "hypothesis": "Thanks for watching!"}]}), encoding="utf-8")
        report = fragment_guard_eval.evaluate(data)
        self.assertEqual(report["sentences_checked"], 4)
        self.assertEqual(report["real_speech_dropped_if_loud"], 1)          # the invented line in the transcripts
        self.assertEqual(report["dropped_examples"][0]["text"], "Thanks for watching!")
        self.assertEqual(report["known_fragments_caught"], report["known_fragments"])
        out = data / "report.json"
        fragment_guard_eval.main(["--data", str(data), "--out", str(out)])
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["sentences_checked"], 4)

    def test_the_committed_measurement_found_no_real_speech_dropped(self):
        results = Path(__file__).resolve().parents[1] / "results" / "fragment_guard.json"
        report = json.loads(results.read_text(encoding="utf-8"))
        self.assertGreater(report["sentences_checked"], 3000)
        self.assertEqual((report["real_speech_dropped_if_loud"], report["real_speech_dropped_if_quiet"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
