import unittest
from eval.evaluate import corpus_bleu, wer

class EvaluationTests(unittest.TestCase):
    def test_wer(self):
        self.assertAlmostEqual(wer([{"reference": "one two three", "hypothesis": "one three"}]), 1 / 3)

    def test_identical_bleu_is_one_hundred(self):
        pair = [{"reference": "this is a complete sentence", "hypothesis": "this is a complete sentence"}]
        self.assertAlmostEqual(corpus_bleu(pair), 100.0)

if __name__ == "__main__": unittest.main()
