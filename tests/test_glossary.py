"""The event glossary (names and terms rendered the same way every time) and the sentences-before context that
help the translator stay consistent."""

import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import glossary
from glossary import Glossary, context_instruction
from translator_worker import Translator, TranslatorWorker

THAI_BROKER = "โบรกเกอร์"
THAI_TERM = "สวัสดี"


def make(**terms):
    return Glossary(terms=terms)


class LoadingTests(unittest.TestCase):
    def test_every_way_of_writing_a_rendering(self):
        g = Glossary(terms={"UIT": "*", "Polyglot Relay": {"*": "Polyglot Relay"}, "broker": {"th": THAI_BROKER},
                            "AI": "A.I.", "Lab": {"*": "*", "my": "Lab-MY"}})
        self.assertEqual(g.rendering("UIT", "th"), "UIT")
        self.assertEqual(g.rendering("Polyglot Relay", "km"), "Polyglot Relay")
        self.assertEqual(g.rendering("broker", "th"), THAI_BROKER)
        self.assertIsNone(g.rendering("broker", "vi"))              # only Thai is fixed
        self.assertEqual(g.rendering("AI", "vi"), "A.I.")
        self.assertEqual((g.rendering("Lab", "my"), g.rendering("Lab", "th")), ("Lab-MY", "Lab"))

    def test_bad_glossaries_are_refused_with_a_reason(self):
        for bad in ([], "x", {"term": 5}, {"term": ["a"]}):
            with self.assertRaises(ValueError):
                Glossary(terms=bad)

    def test_blank_terms_are_skipped_and_spacing_is_tidied(self):
        g = Glossary(terms={"  ": "*", "Polyglot   Relay": "*"})
        self.assertEqual(len(g), 1)
        self.assertEqual(g.rendering("Polyglot Relay", "th"), "Polyglot Relay")

    def test_without_a_file_there_is_no_glossary(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_GLOSSARY_FILE": ""}):
            g = Glossary.from_env()
        self.assertEqual(len(g), 0)
        self.assertEqual(g.matches("anything at UIT", "th"), [])


class FileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "glossary.json")
        patcher = mock.patch.object(glossary, "CHECK_EVERY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, data, raw=None):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps(data, ensure_ascii=False))
        stamp = time.time() + len(str(data)) + (len(raw) if raw else 0)
        os.utime(self.path, (stamp, stamp))                     # a distinct modification time for each write

    def test_it_is_read_from_the_file_named_in_the_environment(self):
        self.write({"UIT": "*"})
        with mock.patch.dict(os.environ, {"POLYGLOT_GLOSSARY_FILE": self.path}):
            g = Glossary.from_env()
        self.assertEqual(g.matches("Welcome to UIT", "th"), [("UIT", "UIT")])

    def test_edits_to_the_file_are_picked_up_without_a_restart(self):
        self.write({"UIT": "*"})
        g = Glossary(self.path)
        version = g.version
        self.write({"UIT": "*", "Relay": "*"})
        self.assertEqual(len(g.matches("UIT Relay", "th")), 2)
        self.assertGreater(g.version, version)

    def test_a_broken_edit_keeps_the_previous_glossary(self):
        self.write({"UIT": "*"})
        g = Glossary(self.path)
        with self.assertLogs("polyglot.glossary", level="WARNING"):
            self.write(None, raw="{not json")
            self.assertEqual(g.matches("at UIT", "th"), [("UIT", "UIT")])

    def test_a_deleted_file_means_no_glossary(self):
        self.write({"UIT": "*"})
        g = Glossary(self.path)
        os.remove(self.path)
        with self.assertLogs("polyglot.glossary", level="WARNING"):
            self.assertEqual(g.matches("at UIT", "th"), [])

    def test_a_missing_file_at_start_is_simply_empty(self):
        self.assertEqual(len(Glossary(self.path)), 0)


class MatchingTests(unittest.TestCase):
    def test_terms_match_regardless_of_case(self):
        self.assertEqual(make(UIT="*").matches("we study at uit today", "th"), [("UIT", "UIT")])

    def test_latin_terms_match_whole_words_only(self):
        g = make(UIT="*", AI="*")
        self.assertEqual(g.matches("fruit and rain", "th"), [])
        self.assertEqual(g.matches("UIT's lab, AI.", "th"), [("UIT", "UIT"), ("AI", "AI")])

    def test_terms_in_scripts_without_spaces_match_inside_text(self):
        g = Glossary(terms={THAI_TERM: {"en": "Sawasdee"}})
        self.assertEqual(g.matches(f"คำว่า{THAI_TERM}ครับ", "en"), [(THAI_TERM, "Sawasdee")])

    def test_multi_word_terms_allow_any_spacing(self):
        self.assertEqual(make(**{"Polyglot Relay": "*"}).matches("the Polyglot\n  Relay demo", "th"), [("Polyglot Relay", "Polyglot Relay")])

    def test_longer_terms_come_first(self):
        g = make(**{"Relay": "*", "Polyglot Relay": "*"})
        self.assertEqual([t for t, _ in g.matches("Polyglot Relay", "th")], ["Polyglot Relay", "Relay"])

    def test_a_term_fixed_only_for_some_languages_is_left_to_the_translator_elsewhere(self):
        g = Glossary(terms={"broker": {"th": THAI_BROKER}})
        self.assertEqual(g.matches("the broker", "th"), [("broker", THAI_BROKER)])
        self.assertEqual(g.matches("the broker", "vi"), [])

    def test_the_gemini_instruction_lists_only_the_terms_in_the_sentence(self):
        g = make(UIT="*", Relay="*")
        note = g.instruction("Welcome to UIT", "th")
        self.assertIn("“UIT” → “UIT”", note)
        self.assertNotIn("Relay", note)
        self.assertEqual(g.instruction("nothing here", "th"), "")


class ProtectionTests(unittest.TestCase):
    def test_terms_become_placeholders_and_come_back_as_their_renderings(self):
        g = Glossary(terms={"UIT": "*", "broker": {"th": THAI_BROKER}})
        protected, restore = g.protect("The broker at UIT and the UIT broker", "th")
        self.assertNotIn("UIT", protected)
        self.assertNotIn("broker", protected)
        self.assertEqual(protected.count(glossary.placeholder(0)) + protected.count(glossary.placeholder(1)), 4)
        translated = protected.replace("The", "X").replace("and the", "Y")
        self.assertEqual(restore(translated).count("UIT"), 2)
        self.assertEqual(restore(translated).count(THAI_BROKER), 2)

    def test_a_placeholder_bent_by_the_translator_is_still_recognised(self):
        g = make(UIT="*")
        _, restore = g.protect("at UIT", "th")
        self.assertEqual(restore("at <X0/>"), "at UIT")
        self.assertEqual(restore("at <x0 />"), "at UIT")
        self.assertEqual(restore("at < x0 / >"), "at UIT")

    def test_a_lost_placeholder_is_reported_so_the_caller_can_retry(self):
        g = make(UIT="*", Lab="*")
        _, restore = g.protect("UIT Lab", "th")
        self.assertIsNone(restore("only " + glossary.placeholder(0)))

    def test_an_unknown_placeholder_number_is_left_alone(self):
        g = make(UIT="*")
        _, restore = g.protect("at UIT", "th")
        self.assertEqual(restore(f"{glossary.placeholder(0)} {glossary.placeholder(7)}"), f"UIT {glossary.placeholder(7)}")

    def test_text_without_terms_is_untouched(self):
        protected, restore = make(UIT="*").protect("nothing to do", "th")
        self.assertEqual(protected, "nothing to do")
        self.assertEqual(restore("rien"), "rien")


class ContextInstructionTests(unittest.TestCase):
    def test_previous_sentences_are_quoted_in_order_with_a_clear_purpose(self):
        note = context_instruction(["First one.", "Second one."])
        self.assertIn("(1) “First one.” (2) “Second one.”", note)
        self.assertIn("translate only the new fragment", note)

    def test_only_the_last_five_are_used_and_long_ones_are_cut(self):
        note = context_instruction([f"s{n}" for n in range(9)] + ["x" * 500])
        self.assertNotIn("“s3”", note)
        self.assertIn("“s5”", note)
        self.assertIn("x" * 300 + "”", note)
        self.assertNotIn("x" * 301, note)

    def test_nothing_is_added_without_context(self):
        for empty in (None, [], ["  "]):
            self.assertEqual(context_instruction(empty), "")


class FakeGemini:
    """Installs a fake google.genai.types and records every request the translator makes."""

    def __init__(self, reply="translated"):
        self.requests = []
        outer = self

        class Config:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class Models:
            def generate_content(self, model, contents, config):
                outer.requests.append({"contents": contents, "instruction": config.system_instruction})
                return types.SimpleNamespace(text=reply)

        self.client = types.SimpleNamespace(models=Models())
        self.types = types.ModuleType("google.genai.types")
        self.types.GenerateContentConfig = Config
        genai = types.ModuleType("google.genai")
        genai.types = self.types
        google = types.ModuleType("google")
        google.genai = genai
        self.modules = {"google": google, "google.genai": genai, "google.genai.types": self.types}


def gemini_translator(lang="th", terms=None):
    with mock.patch.dict(os.environ, {"TRANSLATION_BACKEND": "gemini", "POLYGLOT_GLOSSARY_FILE": ""}):
        translator = Translator(lang)
    translator.glossary = Glossary(terms=terms or {})
    translator.stagger_seconds = 0
    translator._await_gemini_slot = lambda: None
    translator.report_status = lambda *args: None
    return translator


class TranslatorTests(unittest.TestCase):
    def test_gemini_is_given_the_context_and_the_glossary(self):
        fake = FakeGemini()
        translator = gemini_translator(terms={"UIT": "*"})
        translator._get_client = lambda: fake.client
        with mock.patch.dict(sys.modules, fake.modules):
            translated, backend = translator.translate("See you at UIT", source_lang="en", context=["We met yesterday."])
        self.assertEqual((translated, backend), ("translated", "gemini-text"))
        instruction = fake.requests[0]["instruction"]
        self.assertIn("We met yesterday.", instruction)
        self.assertIn("“UIT” → “UIT”", instruction)
        self.assertEqual(fake.requests[0]["contents"], "See you at UIT")        # only the new fragment is translated

    def test_a_name_gemini_transliterated_is_fixed_with_placeholders(self):
        replies = iter(["วิน presents", "<x0/> presents"])        # first ignores the glossary
        fake = FakeGemini()
        fake.client.models.generate_content = lambda model, contents, config: (
            fake.requests.append({"contents": contents, "instruction": config.system_instruction}) or types.SimpleNamespace(text=next(replies)))
        translator = gemini_translator(terms={"Win Pyi Shan Htat": "*"})
        translator._get_client = lambda: fake.client
        with mock.patch.dict(sys.modules, fake.modules):
            translated, _ = translator.translate("Win Pyi Shan Htat presents", source_lang="en")
        self.assertEqual(translated, "Win Pyi Shan Htat presents")
        self.assertEqual(len(fake.requests), 2)
        self.assertIn("<x0/>", fake.requests[1]["contents"])
        self.assertIn("copy every such tag", fake.requests[1]["instruction"])

    def test_a_translation_that_kept_the_terms_costs_one_call(self):
        fake = FakeGemini(reply="Win Pyi Shan Htat presents")
        translator = gemini_translator(terms={"Win Pyi Shan Htat": "*"})
        translator._get_client = lambda: fake.client
        with mock.patch.dict(sys.modules, fake.modules):
            translator.translate("Win Pyi Shan Htat presents", source_lang="en")
        self.assertEqual(len(fake.requests), 1)

    def test_if_the_retry_also_loses_the_term_the_first_answer_is_kept(self):
        replies = iter(["first answer", "mangled"])
        fake = FakeGemini()
        fake.client.models.generate_content = lambda model, contents, config: types.SimpleNamespace(text=next(replies))
        translator = gemini_translator(terms={"UIT": "*"})
        translator._get_client = lambda: fake.client
        with mock.patch.dict(sys.modules, fake.modules):
            self.assertEqual(translator.translate("At UIT", source_lang="en")[0], "first answer")

    def test_without_context_or_terms_the_instruction_is_as_before(self):
        from translator_worker import translation_instruction
        fake = FakeGemini()
        translator = gemini_translator()
        translator._get_client = lambda: fake.client
        with mock.patch.dict(sys.modules, fake.modules):
            translator.translate("hello", source_lang="en")
        self.assertEqual(fake.requests[0]["instruction"], translation_instruction("th", "en"))

    def test_the_cache_ignores_context_but_not_a_changed_glossary(self):
        fake = FakeGemini(reply="O UIT")                      # keeps the term, so one call per translation
        translator = gemini_translator(terms={"UIT": "*"})
        translator._get_client = lambda: fake.client
        with mock.patch.dict(sys.modules, fake.modules):
            translator.translate("At UIT", source_lang="en", context=["a"])
            self.assertEqual(translator.translate("At UIT", source_lang="en", context=["b"])[1], "cache")
            translator.glossary._install({"UIT": {"th": "O UIT"}})             # the organisers changed a term
            self.assertEqual(translator.translate("At UIT", source_lang="en")[1], "gemini-text")
        self.assertEqual(len(fake.requests), 2)

    def libre(self, reply):
        with mock.patch.dict(os.environ, {"TRANSLATION_BACKEND": "libretranslate", "POLYGLOT_GLOSSARY_FILE": ""}):
            translator = Translator("vi")
        translator.glossary = Glossary(terms={"UIT": "*", "broker": {"vi": "BROKER-VI"}})
        translator._can_use_libretranslate = lambda: True
        sent = []

        def fake(text, source_lang=None):
            sent.append(text)
            return reply(text)
        translator._translate_via_libretranslate = fake
        return translator, sent

    def test_libretranslate_gets_placeholders_and_the_renderings_are_put_back(self):
        translator, sent = self.libre(lambda text: text.replace("The", "Cai").replace("at", "o"))
        translated, backend = translator.translate("The broker at UIT", source_lang="en")
        self.assertEqual(backend, "libretranslate")
        self.assertNotIn("UIT", sent[0])
        self.assertEqual(translated, "Cai BROKER-VI o UIT")

    def test_if_libretranslate_mangles_a_placeholder_it_translates_plainly_instead(self):
        translator, sent = self.libre(lambda text: "mangled" if "<x" in text else "plain translation")
        translated, _ = translator.translate("The broker at UIT", source_lang="en")
        self.assertEqual(translated, "plain translation")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1], "The broker at UIT")

    def test_a_sentence_without_terms_is_sent_once_as_it_is(self):
        translator, sent = self.libre(lambda text: "ok")
        translator.translate("Nothing special", source_lang="en")
        self.assertEqual(sent, ["Nothing special"])


class WorkerContextTests(unittest.TestCase):
    def test_the_worker_passes_the_context_from_the_chunk_to_the_translator(self):
        worker = TranslatorWorker("th", "127.0.0.1", 0, "th-test", mock=True)
        worker.translator.translate = mock.Mock(return_value=("t", "mock"))
        worker._send_broker = mock.Mock()
        worker._process_chunk(mock.Mock(), {"chunk_id": "c1", "text": "now", "context": ["before"]})
        self.assertEqual(worker.translator.translate.call_args.kwargs["context"], ["before"])
        worker._process_chunk(mock.Mock(), {"chunk_id": "c2", "text": "now", "context": "not a list"})
        self.assertNotIn("context", worker.translator.translate.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
