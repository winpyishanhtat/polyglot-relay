# Accuracy on real speech: FLEURS test split, 150 clips per language

Measured 2026-10-13 on the relay's own pipeline (Gemini `gemini-flash-lite-latest` for Burmese, Khmer and
Lao; Whisper `base` for the other six; translation into English by Gemini). Data: Google FLEURS
(google/fleurs, CC-BY-4.0), the first 150 recordings of each language's test archive, 1,350 clips in all.

| Language | Clips | CER % (95% CI) | WER % | Empty | Median s | RTF | chrF++ from reference | chrF++ end to end |
|---|---|---|---|---|---|---|---|---|
| en | 150 | 5.33 (4.51-6.27) | 10.74 (9.23-12.47) | 0 | 3.8 | 0.446 | - | - |
| my | 150 | 15.71 (13.94-17.91) | n/a | 0 | 4.92 | 0.368 | 56.58 (54.51-58.67) | 44.23 (42.11-46.22) |
| id | 150 | 13.46 (11.37-15.94) | 39.0 (35.58-42.61) | 0 | 4.43 | 0.552 | 68.38 (66.25-70.43) | 52.24 (49.6-54.74) |
| ms | 150 | 12.78 (11.07-14.83) | 38.91 (35.98-42.15) | 1 | 4.45 | 0.511 | 68.87 (67.02-70.79) | 52.93 (50.49-55.53) |
| th | 150 | 40.98 (38.13-43.97) | n/a | 0 | 7.83 | 1.273 | 59.77 (58.02-61.52) | 30.29 (28.62-32.17) |
| vi | 150 | 26.64 (24.22-29.36) | 43.51 (40.45-46.97) | 0 | 5.21 | 0.84 | 62.55 (60.21-64.96) | 38.72 (36.49-41.17) |
| tl | 150 | 15.93 (14.25-17.72) | 52.51 (49.62-55.68) | 0 | 4.3 | 0.304 | 70.55 (68.26-72.84) | 51.78 (49.43-53.93) |
| km | 150 | 21.51 (19.68-23.56) | n/a | 0 | 5.17 | 0.396 | 61.82 (59.42-64.14) | 51.3 (49.14-53.48) |
| lo | 150 | 17.64 (14.79-20.76) | n/a | 0 | 4.52 | 0.422 | 61.76 (59.84-63.77) | 51.89 (49.69-54.22) |

- **CER / WER**: character / word error rate after Unicode normalisation and punctuation removal, summed over
  all clips; 95% bootstrap intervals over clips (1,000 resamples, seed 0). A clip that produced no text counts as 100%.
  WER is not reported (n/a) for Burmese, Thai, Khmer and Lao because those scripts do not separate words with spaces.
- **chrF++ from reference**: translating the human transcript into English, scored against the human English
  translation (isolates translation quality). **End to end**: translating the recognised text instead.
  chrF++ matches sacrebleu 2.x.
- **Median s / RTF**: seconds to transcribe one clip and processing time over audio time. Two Whisper containers
  shared one CPU, so RTF is pessimistic; Thai above 1.0 would not keep up with live speech on this machine.

## Limits of these numbers
- Each language was scored on its **own** first 150 clips, so sentence difficulty differs between languages;
  compare languages loosely. Read-aloud speech, not conversation; one model per language, one run.
- Whisper `base` is the relay's default, not the best Whisper. Thai (41% CER) and Vietnamese (27%) are weak mainly
  for that reason; no other Whisper size or Gemini transcription was tried for those languages.
- Earlier tests with synthesized speech gave 3-4% CER for Burmese. On real speakers it is 15.7%, so synthesized
  audio overstated accuracy about fourfold.
- Errors are spread across clips (median clip CER is close to the mean) rather than a few failures; output length
  matches the reference (ratio 0.94-1.01), so there is no systematic dropping or inventing of text, except Thai
  (32 of 150 clips above 50% CER) and Lao (8 clips above 50%).
- Per-clip outputs are not committed (about 3 MB); `eval.prepare_fleurs` and `eval.fleurs_eval` regenerate them.
