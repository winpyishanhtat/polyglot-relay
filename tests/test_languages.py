import os
import unittest
from unittest import mock

from common import config
from translator_worker import Translator, translation_instruction


class TranslationInstructionTests(unittest.TestCase):
    """Covers the Gemini system_instruction text itself: it must frame the
    task as live spoken captioning (not document translation), and give
    Khmer/Lao specifically an extra care nudge - Burmese, despite also
    being outside LibreTranslate's catalog, translates noticeably better
    in practice and doesn't need it.
    """

    def test_mentions_live_captioning_context_and_fragment_handling(self):
        instruction = translation_instruction("th", "en")
        self.assertIn("live", instruction.lower())
        self.assertIn("incomplete sentence", instruction)
        self.assertIn("natural, conversational", instruction)

    def test_low_resource_languages_get_the_extra_care_note(self):
        for lang in ("km", "lo"):
            with self.subTest(lang=lang):
                self.assertIn("less training data", translation_instruction(lang, "en"))

    def test_other_languages_do_not_get_the_care_note(self):
        for lang in ("my", "th", "vi", "en"):
            with self.subTest(lang=lang):
                self.assertNotIn("less training data", translation_instruction(lang, "en"))

    def test_auto_detect_variant_asks_the_model_to_identify_the_language(self):
        instruction = translation_instruction("th", source_lang=None)
        self.assertIn("Identify the spoken language", instruction)


class LanguageRegistryTests(unittest.TestCase):
    """Guards the bug where TARGET_LANGUAGES and the translation-prompt name
    table could drift apart: adding a language to one without the other used
    to raise KeyError deep inside a worker thread, which looked from the
    dashboard like a translation pool that was connected but silently doing
    nothing.
    """

    def test_every_target_language_has_metadata(self):
        for code in config.TARGET_LANGUAGES:
            with self.subTest(code=code):
                label = config.language_label(code)
                self.assertTrue(label)

    def test_mock_translation_works_for_every_registered_language(self):
        for code in config.TARGET_LANGUAGES:
            with self.subTest(code=code):
                text, backend = Translator(code, mock=True).translate("hello")
                self.assertEqual(backend, "mock")
                self.assertTrue(text)

    def test_asean_language_set_is_present_and_exclusive(self):
        # English (ASEAN's working language, covers Singapore) plus one
        # representative language per remaining ASEAN member state (Malay
        # covers Brunei) - every entry shares the "ASEAN" group now that
        # the project no longer targets non-ASEAN languages.
        for code in ("en", "my", "id", "ms", "th", "vi", "tl", "km", "lo"):
            self.assertEqual(config.LANGUAGES[code]["group"], "ASEAN")
        self.assertEqual({meta["group"] for meta in config.LANGUAGES.values()}, {"ASEAN"})
        # guard against accidentally re-adding the languages this build
        # deliberately dropped (Japanese, and the UN-only languages)
        for code in ("ja", "ar", "zh", "fr", "ru", "es"):
            self.assertNotIn(code, config.LANGUAGES)


class TextPathIsDefaultTests(unittest.TestCase):
    """Regression test for the "translation pools not working" bug: when raw
    audio is attached to every chunk, translate() always takes the slower,
    more failure-prone Gemini Live path instead of the plain-text path, even
    though Whisper already produced an accurate transcript. stt_service.py
    must only attach audio when explicitly asked to.
    """

    def test_translate_uses_text_path_when_no_audio_is_attached(self):
        # audio_pcm_b64=None must route to _translate_text, not _translate_live
        translator = Translator("id", mock=True)
        text, backend = translator.translate("hello", audio_pcm_b64=None)
        self.assertEqual(backend, "mock")
        self.assertEqual(text, "[id] hello")


class SourceLanguageTests(unittest.TestCase):
    """Covers the "any language to multiple languages" feature: a caption's
    source language can now be declared explicitly instead of every worker
    assuming English. When a worker's own language matches the declared
    source, it must skip translation entirely (a passthrough) rather than
    round-tripping through Gemini to "translate" a language into itself.
    """

    def test_mock_defaults_to_english_source_for_backward_compatibility(self):
        # no source_lang passed at all - must match the original hardcoded
        # "en is the implicit source" behavior exactly.
        self.assertEqual(Translator("en", mock=True).translate("hi"), ("hi", "mock"))
        self.assertEqual(Translator("th", mock=True).translate("hi"), ("[th] hi", "mock"))

    def test_mock_passthrough_for_any_declared_source_language(self):
        # Indonesian speaker, Indonesian worker -> unprefixed passthrough, not "[id] ..."
        text, backend = Translator("id", mock=True).translate("halo", source_lang="id")
        self.assertEqual((text, backend), ("halo", "mock"))
        # Indonesian speaker, Thai worker -> still gets translated (prefixed in mock)
        text, backend = Translator("th", mock=True).translate("halo", source_lang="id")
        self.assertEqual((text, backend), ("[th] halo", "mock"))

    def test_real_mode_passthrough_needs_no_api_key(self):
        # Same-language passthrough must short-circuit before ever touching
        # the Gemini client, so it works with no GEMINI_API_KEY configured.
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": ""}):
            translator = Translator("vi", mock=False)
            text, backend = translator.translate("xin chao", source_lang="vi")
        self.assertEqual((text, backend), ("xin chao", "passthrough"))

    def test_auto_source_never_shortcuts_to_passthrough(self):
        # "auto" means "unknown, let the translator detect it" - must not be
        # treated as a match even when it happens to equal the worker's own
        # code. Stub out both backends so this doesn't depend on the
        # google-genai SDK or a reachable LibreTranslate instance.
        translator = Translator("vi", mock=False)
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=False), \
             mock.patch.object(translator, "_translate_text", return_value="stubbed") as stub:
            text, backend = translator.translate("hello", source_lang="auto")
        stub.assert_called_once()
        self.assertEqual((text, backend), ("stubbed", "gemini-text"))


class GeminiTierTests(unittest.TestCase):
    """Covers GEMINI_TIER, which picks sane defaults for a paid/billing-enabled
    API key (the default) vs. the original free-tier-safe behavior: the paid
    tier's higher RPM ceiling means a 15-way broadcast no longer needs to be
    staggered, and the higher-quality Pro model is affordable to use by
    default. An explicitly-set GEMINI_TEXT_MODEL or
    POLYGLOT_STAGGER_STEP_SECONDS must still win over either tier's default.
    """

    def test_paid_tier_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for var in ("GEMINI_TIER", "GEMINI_TEXT_MODEL", "POLYGLOT_STAGGER_STEP_SECONDS"):
                os.environ.pop(var, None)
            translator = Translator("th", mock=True)
        self.assertEqual(translator.tier, "paid")
        self.assertEqual(translator.text_model, "gemini-pro-latest")
        self.assertEqual(translator.stagger_seconds, 0.0)

    def test_free_tier_restores_original_stagger_and_flash_model(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "free"}):
            os.environ.pop("GEMINI_TEXT_MODEL", None)
            os.environ.pop("POLYGLOT_STAGGER_STEP_SECONDS", None)
            translator = Translator("th", mock=True)
        self.assertEqual(translator.tier, "free")
        self.assertEqual(translator.text_model, "gemini-3.6-flash")
        index = config.TARGET_LANGUAGES.index("th")
        self.assertAlmostEqual(translator.stagger_seconds, index * 0.08)

    def test_blank_env_vars_fall_back_to_tier_defaults(self):
        # Docker Compose commonly passes an unset host var through as an
        # empty string (${VAR:-}) rather than omitting it - that must fall
        # back to the tier default exactly like a fully unset var does.
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "", "GEMINI_TEXT_MODEL": "",
                                           "GEMINI_LIVE_MODEL": ""}):
            translator = Translator("th", mock=True)
        self.assertEqual(translator.tier, "paid")
        self.assertEqual(translator.text_model, "gemini-pro-latest")
        self.assertEqual(translator.live_model, "gemini-3.5-live-translate-preview")

    def test_explicit_model_and_stagger_override_either_tier(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "paid", "GEMINI_TEXT_MODEL": "gemini-3.6-flash",
                                           "POLYGLOT_STAGGER_STEP_SECONDS": "0.5"}):
            translator = Translator("th", mock=True)
        self.assertEqual(translator.text_model, "gemini-3.6-flash")
        index = config.TARGET_LANGUAGES.index("th")
        self.assertAlmostEqual(translator.stagger_seconds, index * 0.5)


class GeminiRateGateTests(unittest.TestCase):
    """Covers _await_gemini_slot, the worker-side half of the shared rate
    gate (see gemini_rate_gate.py for the server side): it must only ever
    engage on the free tier with a gate URL configured, and a gate that's
    unreachable must be a warning, never a reason to fail translation.
    """

    def test_paid_tier_never_calls_the_gate_even_if_url_is_set(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "paid", "GEMINI_RATE_GATE_URL": "http://gate:8090"}):
            translator = Translator("th", mock=True)
        with mock.patch("translator_worker.urllib.request.urlopen") as urlopen:
            translator._await_gemini_slot()
        urlopen.assert_not_called()

    def test_free_tier_without_gate_url_configured_is_a_no_op(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "free"}):
            os.environ.pop("GEMINI_RATE_GATE_URL", None)
            translator = Translator("th", mock=True)
        with mock.patch("translator_worker.urllib.request.urlopen") as urlopen:
            translator._await_gemini_slot()
        urlopen.assert_not_called()

    def test_free_tier_with_gate_url_blocks_on_acquire(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "free", "GEMINI_RATE_GATE_URL": "http://gate:8090/"}):
            translator = Translator("th", mock=True)
        with mock.patch("translator_worker.urllib.request.urlopen") as urlopen:
            translator._await_gemini_slot()
        urlopen.assert_called_once()
        (called_url,), _kwargs = urlopen.call_args
        # Trailing slash on the configured URL must not produce a double
        # slash in the request path.
        self.assertEqual(called_url, "http://gate:8090/acquire")

    def test_unreachable_gate_does_not_bypass_request_budget(self):
        with mock.patch.dict(os.environ, {"GEMINI_TIER": "free", "GEMINI_RATE_GATE_URL": "http://gate:8090"}):
            translator = Translator("th", mock=True)
        with mock.patch("translator_worker.urllib.request.urlopen", side_effect=OSError("unreachable")):
            with self.assertRaisesRegex(RuntimeError, "request was not sent"):
                translator._await_gemini_slot()


class RetryLogicTests(unittest.TestCase):
    """Regression coverage for the "only 3-4 languages work" bug: a single
    caption fans out to all 15 language workers at roughly the same instant
    (broker.route_chunk), which was enough to trip Gemini's free-tier rate
    limit for every worker beyond the first handful. Failed calls must retry
    with backoff instead of giving up after one attempt.
    """

    def test_retryable_status_codes_are_detected(self):
        from translator_worker import _is_retryable

        class FakeError(Exception):
            def __init__(self, code):
                super().__init__(f"{code} error")
                self.code = code

        self.assertTrue(_is_retryable(FakeError(429)))
        self.assertTrue(_is_retryable(FakeError(503)))
        self.assertFalse(_is_retryable(FakeError(401)))
        self.assertTrue(_is_retryable(RuntimeError("429 RESOURCE_EXHAUSTED: quota")))
        self.assertFalse(_is_retryable(RuntimeError("400 INVALID_ARGUMENT")))
        # Bare httpx connection-level failures - transient network blips,
        # not anything Gemini-specific, but just as worth retrying.
        self.assertTrue(_is_retryable(RuntimeError("Server disconnected without sending a response.")))
        self.assertTrue(_is_retryable(RuntimeError("Connection reset by peer")))

    def test_suggested_retry_delay_extracts_googles_own_hint(self):
        from translator_worker import _suggested_retry_delay

        exc = RuntimeError(
            "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, "
            "'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
            "'retryDelay': '20.755201785s'}]}}"
        )
        self.assertAlmostEqual(_suggested_retry_delay(exc), 20.755201785)
        self.assertIsNone(_suggested_retry_delay(RuntimeError("no hint here")))

    def test_call_with_retry_waits_at_least_googles_suggested_delay(self):
        # Regression coverage: our own exponential backoff starts far
        # shorter (~1-1.5s) than the per-minute window Google says is still
        # active (often 20-50s) - retrying on our own schedule alone just
        # re-fails against the same window, wasting an attempt (and scarce
        # daily quota) before the limit has actually lifted.
        translator = Translator("en", mock=False)
        translator.max_retries = 2

        class RateLimited(Exception):
            code = 429

            def __str__(self):
                return "429 RESOURCE_EXHAUSTED ... 'retryDelay': '20s' ..."

        def always_rate_limited():
            raise RateLimited()

        with mock.patch("translator_worker.time.sleep") as sleep:
            with self.assertRaises(RateLimited):
                translator._call_with_retry(always_rate_limited)
        waited = sleep.call_args[0][0]
        self.assertGreaterEqual(waited, 20.0)

    def test_call_with_retry_recovers_from_transient_failures(self):
        translator = Translator("en", mock=False)
        translator.max_retries = 4
        attempts = {"count": 0}

        class RateLimited(Exception):
            code = 429

        def flaky():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RateLimited("rate limited")
            return "ok"

        with mock.patch("translator_worker.time.sleep"):
            result = translator._call_with_retry(flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)

    def test_call_with_retry_gives_up_on_non_retryable_errors(self):
        translator = Translator("en", mock=False)
        attempts = {"count": 0}

        def always_denied():
            attempts["count"] += 1
            err = Exception("denied")
            err.code = 401
            raise err

        with mock.patch("translator_worker.time.sleep"):
            with self.assertRaises(Exception):
                translator._call_with_retry(always_denied)
        self.assertEqual(attempts["count"], 1)  # no retries for a non-retryable error

    def test_call_with_retry_does_not_retry_daily_quota_exhaustion(self):
        # Regression test: a 429 whose quotaId says PerDay must fail on the
        # first attempt, not burn through max_retries - Google's suggested
        # retryDelay for these is misleadingly short (seconds), but the
        # quota genuinely will not reset for hours.
        translator = Translator("en", mock=False)
        attempts = {"count": 0}

        def daily_quota_exhausted():
            attempts["count"] += 1
            err = Exception(
                "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': "
                "[{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}"
            )
            err.code = 429
            raise err

        with mock.patch("translator_worker.time.sleep") as sleep_mock:
            with self.assertRaises(Exception):
                translator._call_with_retry(daily_quota_exhausted)
        self.assertEqual(attempts["count"], 1)
        sleep_mock.assert_not_called()

    def test_call_with_retry_does_retry_a_perday_error_that_has_a_delay(self):
        # Confirmed on a real billing-enabled key: a "PerDay"-labeled 429
        # that also carries Google's own short retryDelay recovered on the
        # very next attempt, unlike the delay-less case above which really
        # doesn't clear for hours. Treating every "PerDay" label as
        # unrecoverable was overly conservative and dropped translations
        # that a single retry would have saved.
        translator = Translator("en", mock=False)
        translator.max_retries = 2
        attempts = {"count": 0}

        def perday_with_delay():
            attempts["count"] += 1
            if attempts["count"] < 2:
                err = Exception(
                    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': "
                    "[{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}, "
                    "{'retryDelay': '13s'}]}}"
                )
                err.code = 429
                raise err
            return "ok"

        with mock.patch("translator_worker.time.sleep") as sleep_mock:
            result = translator._call_with_retry(perday_with_delay)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 2)
        sleep_mock.assert_called_once()


class LibreTranslateBackendTests(unittest.TestCase):
    """Covers the pluggable backend: LibreTranslate (free, self-hosted) is
    tried first for a language it has loaded, Gemini fills in the rest, and
    TRANSLATION_BACKEND can pin either one explicitly.
    """

    def test_prefers_libretranslate_when_language_is_supported(self):
        translator = Translator("th", mock=False)
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=True), \
             mock.patch.object(translator, "_translate_via_libretranslate", return_value="translated") as lt, \
             mock.patch.object(translator, "_translate_text") as gemini_stub:
            text, backend = translator.translate("hello", source_lang="en")
        lt.assert_called_once()
        gemini_stub.assert_not_called()
        self.assertEqual((text, backend), ("translated", "libretranslate"))

    def test_falls_back_to_gemini_when_libretranslate_call_fails(self):
        translator = Translator("th", mock=False)
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=True), \
             mock.patch.object(translator, "_translate_via_libretranslate", side_effect=RuntimeError("down")), \
             mock.patch.object(translator, "_translate_text", return_value="stubbed") as gemini_stub:
            text, backend = translator.translate("hello", source_lang="en")
        gemini_stub.assert_called_once()
        self.assertEqual((text, backend), ("stubbed", "gemini-text"))

    def test_falls_back_to_gemini_when_language_not_loaded(self):
        translator = Translator("my", mock=False)
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=False), \
             mock.patch.object(translator, "_translate_via_libretranslate") as lt, \
             mock.patch.object(translator, "_translate_text", return_value="stubbed") as gemini_stub:
            text, backend = translator.translate("hello", source_lang="en")
        lt.assert_not_called()
        gemini_stub.assert_called_once()
        self.assertEqual((text, backend), ("stubbed", "gemini-text"))

    def test_never_supported_languages_skip_the_libretranslate_check_entirely(self):
        # my/km/lo are permanently absent from Argos Translate's catalog -
        # not a config choice that might change, so there's nothing to
        # gain from ever asking LibreTranslate about them (not even a
        # cached "no" once a minute).
        for lang in ("my", "km", "lo"):
            translator = Translator(lang, mock=False)
            with mock.patch.object(translator, "_libretranslate_supports_target") as lt_check, \
                 mock.patch.object(translator, "_translate_text", return_value="stubbed") as gemini_stub:
                text, backend = translator.translate("hello", source_lang="en")
            lt_check.assert_not_called()
            gemini_stub.assert_called_once()
            self.assertEqual((text, backend), ("stubbed", "gemini-text"))

    def test_backend_gemini_skips_libretranslate_entirely(self):
        translator = Translator("th", mock=False)
        translator.backend_preference = "gemini"
        with mock.patch.object(translator, "_libretranslate_supports_target") as lt_check, \
             mock.patch.object(translator, "_translate_text", return_value="stubbed") as gemini_stub:
            text, backend = translator.translate("hello", source_lang="en")
        lt_check.assert_not_called()
        gemini_stub.assert_called_once()
        self.assertEqual((text, backend), ("stubbed", "gemini-text"))

    def test_backend_libretranslate_raises_instead_of_falling_back(self):
        translator = Translator("my", mock=False)
        translator.backend_preference = "libretranslate"
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=False), \
             mock.patch.object(translator, "_translate_text") as gemini_stub:
            with self.assertRaises(RuntimeError):
                translator.translate("hello", source_lang="en")
        gemini_stub.assert_not_called()

    def test_supports_target_caches_and_recovers_from_connection_failure(self):
        translator = Translator("th", mock=False)
        with mock.patch("translator_worker._libretranslate_get", side_effect=OSError("connection refused")):
            self.assertFalse(translator._libretranslate_supports_target())
        # a still-loading LibreTranslate must not be cached as permanently
        # unsupported - once it answers, the very next check (after the 60s
        # cool-down) must pick that up.
        translator._lt_checked_at = 0  # force the cache to be treated as stale
        with mock.patch("translator_worker._libretranslate_get",
                         return_value=[{"code": "th", "name": "Thai"}]):
            self.assertTrue(translator._libretranslate_supports_target())


class EnglishPivotTranslationTests(unittest.TestCase):
    """LibreTranslate cannot read Burmese/Khmer/Lao. Without a pivot every
    worker made its own Gemini call (8 per chunk); with the sender's shared
    English rendering, only Gemini-only targets still need one."""

    BURMESE = "ဟာ ထမင်းစားမလို့"

    def _translate(self, lang, *, pivot, source="my", backend=None, lt_return="from-lt"):
        translator = Translator(lang, mock=False)
        if backend:
            translator.backend_preference = backend
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=True), \
             mock.patch.object(translator, "_translate_via_libretranslate", return_value=lt_return) as lt, \
             mock.patch.object(translator, "_translate_text", return_value="from-gemini") as gemini:
            result = translator.translate(self.BURMESE, source_lang=source, pivot_en=pivot)
        return result, lt, gemini

    def test_english_pool_reuses_the_pivot_without_any_call(self):
        result, lt, gemini = self._translate("en", pivot="I am about to eat")
        self.assertEqual(result, ("I am about to eat", "pivot"))
        lt.assert_not_called()
        gemini.assert_not_called()

    def test_libretranslate_languages_translate_from_the_english_pivot(self):
        result, lt, gemini = self._translate("th", pivot="I am about to eat")
        self.assertEqual(result, ("from-lt", "libretranslate-pivot"))
        lt.assert_called_once_with("I am about to eat", "en")
        gemini.assert_not_called()

    def test_a_failing_pivot_call_falls_back_to_direct_gemini_from_the_original_text(self):
        translator = Translator("th", mock=False)
        with mock.patch.object(translator, "_libretranslate_supports_target", return_value=True), \
             mock.patch.object(translator, "_translate_via_libretranslate", side_effect=RuntimeError("down")), \
             mock.patch.object(translator, "_translate_text", return_value="from-gemini") as gemini:
            result = translator.translate(self.BURMESE, source_lang="my", pivot_en="I am about to eat")
        self.assertEqual(result, ("from-gemini", "gemini-text"))
        gemini.assert_called_once_with(self.BURMESE, "my")

    def test_gemini_only_targets_translate_directly_from_the_original(self):
        for lang in ("km", "lo"):
            result, lt, gemini = self._translate(lang, pivot="I am about to eat")
            self.assertEqual(result, ("from-gemini", "gemini-text"))
            lt.assert_not_called()
            gemini.assert_called_once_with(self.BURMESE, "my")

    def test_without_a_pivot_libretranslate_is_not_asked_to_read_burmese(self):
        # The wasted-retries regression: it used to be tried (and fail) first.
        result, lt, gemini = self._translate("th", pivot=None)
        self.assertEqual(result, ("from-gemini", "gemini-text"))
        lt.assert_not_called()

    def test_a_readable_source_is_unchanged_and_ignores_any_pivot(self):
        result, lt, gemini = self._translate("id", pivot="ignored", source="th")
        self.assertEqual(result, ("from-lt", "libretranslate"))
        lt.assert_called_once_with(self.BURMESE, "th")

    def test_libretranslate_only_mode_refuses_an_unreadable_source_with_a_clear_error(self):
        with self.assertRaises(RuntimeError) as caught:
            self._translate("th", pivot=None, backend="libretranslate")
        self.assertIn("cannot read the source language", str(caught.exception))

    def test_libretranslate_only_mode_still_uses_the_pivot(self):
        result, _, _ = self._translate("th", pivot="I am about to eat", backend="libretranslate")
        self.assertEqual(result[1], "libretranslate-pivot")


if __name__ == "__main__":
    unittest.main()
