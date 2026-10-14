"""A phrase translated once is answered from memory the next time: no API call, no quota."""

import threading
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

from translator_worker import Translator


def make(lang="th", **env):
    with mock.patch.dict("os.environ", {"TRANSLATION_BACKEND": "gemini", "GEMINI_API_KEY": "test-key", **env}):
        translator = Translator(lang)
    translator.calls = []

    def fake_text(text, source=None):
        translator.calls.append((text, source))
        return f"<{lang}:{text}>"
    translator._translate_text = fake_text
    return translator


class CacheBehaviourTests(unittest.TestCase):
    def test_the_second_time_a_phrase_is_answered_from_memory(self):
        t = make()
        first = t.translate("Thank you", source_lang="en")
        second = t.translate("Thank you", source_lang="en")
        self.assertEqual(first, ("<th:Thank you>", "gemini-text"))
        self.assertEqual(second, ("<th:Thank you>", "cache"))
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(t.cache_hits, 1)

    def test_whitespace_differences_do_not_defeat_the_cache(self):
        t = make()
        t.translate("Thank you", source_lang="en")
        self.assertEqual(t.translate("  Thank   you \n", source_lang="en")[1], "cache")
        self.assertEqual(len(t.calls), 1)

    def test_different_wording_or_case_is_a_different_phrase(self):
        t = make()
        t.translate("Thank you", source_lang="en")
        for other in ("thank you", "Thank you.", "Thanks"):
            self.assertNotEqual(t.translate(other, source_lang="en")[1], "cache", other)
        self.assertEqual(len(t.calls), 4)

    def test_the_spoken_language_is_part_of_the_key(self):
        t = make()
        t.translate("bank", source_lang="en")
        self.assertNotEqual(t.translate("bank", source_lang="id")[1], "cache")
        self.assertNotEqual(t.translate("bank")[1], "cache")                       # auto-detect is its own key
        self.assertEqual(t.translate("bank", source_lang="id")[1], "cache")

    def test_a_shared_english_rendering_is_part_of_the_key(self):
        t = make("th", TRANSLATION_BACKEND="gemini")
        t.translate("\u1019", source_lang="my", pivot_en="Hello")
        self.assertNotEqual(t.translate("\u1019", source_lang="my", pivot_en="Hi there")[1], "cache")

    def test_failures_are_not_remembered(self):
        t = make()
        outcomes = iter([RuntimeError("quota"), "fine"])

        def flaky(text, source=None):
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result
        t._translate_text = flaky
        with self.assertRaises(RuntimeError):
            t.translate("Hello", source_lang="en")
        self.assertEqual(t.translate("Hello", source_lang="en"), ("fine", "gemini-text"))

    def test_an_empty_result_is_never_cached(self):
        t = make()
        answers = iter(["", "real"])
        t._translate_text = lambda text, source=None: next(answers)
        t.translate("Hello", source_lang="en")
        self.assertEqual(t.translate("Hello", source_lang="en")[0], "real")

    def test_the_oldest_entries_are_evicted_first(self):
        t = make(POLYGLOT_TRANSLATION_CACHE="3")
        for word in ("a", "b", "c"):
            t.translate(word, source_lang="en")
        t.translate("a", source_lang="en")           # a is now the most recently used
        t.translate("d", source_lang="en")           # evicts b, the least recently used
        t.calls.clear()
        self.assertEqual(t.translate("a", source_lang="en")[1], "cache")
        self.assertEqual(t.translate("c", source_lang="en")[1], "cache")
        self.assertNotEqual(t.translate("b", source_lang="en")[1], "cache")
        self.assertEqual(t.calls, [("b", "en")])

    def test_the_default_size_is_five_hundred_and_an_empty_setting_means_the_default(self):
        self.assertEqual(make(POLYGLOT_TRANSLATION_CACHE="")._cache_size, 500)
        self.assertEqual(make(POLYGLOT_TRANSLATION_CACHE="25")._cache_size, 25)
        self.assertEqual(make(POLYGLOT_TRANSLATION_CACHE="-5")._cache_size, 0)

    def test_zero_turns_the_cache_off(self):
        t = make(POLYGLOT_TRANSLATION_CACHE="0")
        t.translate("Hello", source_lang="en")
        self.assertEqual(t.translate("Hello", source_lang="en")[1], "gemini-text")
        self.assertEqual(len(t.calls), 2)

    def test_the_cache_is_per_worker_so_languages_never_share_answers(self):
        thai, khmer = make("th"), make("km")
        thai.translate("Hello", source_lang="en")
        self.assertEqual(khmer.translate("Hello", source_lang="en"), ("<km:Hello>", "gemini-text"))

    def test_same_language_text_and_mock_mode_never_touch_the_cache(self):
        t = make("th")
        self.assertEqual(t.translate("\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35", source_lang="th")[1], "passthrough")
        self.assertEqual(t.cache_hits, 0)
        mock_translator = Translator("th", mock=True)
        self.assertEqual(mock_translator.translate("hi", source_lang="en")[1], "mock")
        self.assertEqual(mock_translator.translate("hi", source_lang="en")[1], "mock")

    def test_the_english_pool_reusing_a_pivot_stays_a_pivot(self):
        t = make("en")
        self.assertEqual(t.translate("\u1019", source_lang="my", pivot_en="Hello"), ("Hello", "pivot"))
        self.assertEqual(t.translate("\u1019", source_lang="my", pivot_en="Hello")[1], "pivot")

    def test_a_cached_answer_is_returned_even_when_the_api_would_now_fail(self):
        t = make()
        t.translate("Welcome", source_lang="en")
        t._translate_text = mock.Mock(side_effect=RuntimeError("quota exhausted"))
        self.assertEqual(t.translate("Welcome", source_lang="en"), ("<th:Welcome>", "cache"))
        t._translate_text.assert_not_called()

    def test_libretranslate_answers_are_cached_too(self):
        with mock.patch.dict("os.environ", {"TRANSLATION_BACKEND": "auto"}):
            t = Translator("id")
        t._can_use_libretranslate = lambda: True
        calls = []
        t._translate_via_libretranslate = lambda text, source=None: calls.append(text) or "Halo"
        self.assertEqual(t.translate("Hello", source_lang="en"), ("Halo", "libretranslate"))
        self.assertEqual(t.translate("Hello", source_lang="en"), ("Halo", "cache"))
        self.assertEqual(calls, ["Hello"])

    def test_concurrent_use_stays_consistent_and_within_the_limit(self):
        t = make(POLYGLOT_TRANSLATION_CACHE="50")
        errors = []

        def hammer(offset):
            try:
                for n in range(200):
                    t.translate(f"phrase {(n + offset) % 80}", source_lang="en")
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=hammer, args=(i * 7,)) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(t._cache), 50)


if __name__ == "__main__":
    unittest.main()
