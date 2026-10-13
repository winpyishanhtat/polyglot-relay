# Accuracy on real speech: FLEURS test split, 150 clips per language

Measured 2026-10-13 on the relay's own pipeline (Gemini `gemini-flash-lite-latest` for Burmese, Khmer and
Lao; Whisper `base` for the other six; translation into English by Gemini). Data: Google FLEURS
(google/fleurs, CC-BY-4.0), the first 150 recordings of each language's test archive, 1,350 clips in all.

| Language | Clips | CER % (95% CI) | WER % | Empty | Median s | RTF | chrF++ from reference | chrF++ end to end | CER % if Gemini transcribes |
|---|---|---|---|---|---|---|---|---|---|
| en | 150 | 5.33 (4.51-6.27) | 10.74 (9.23-12.47) | 0 | 3.8 | 0.446 | - | - | 2.61 (1.95-3.37) |
| my | 150 | 15.71 (13.94-17.91) | n/a | 0 | 4.92 | 0.368 | 56.58 (54.51-58.67) | 44.23 (42.11-46.22) | - |
| id | 150 | 13.46 (11.37-15.94) | 39.0 (35.58-42.61) | 0 | 4.43 | 0.552 | 68.38 (66.25-70.43) | 52.24 (49.6-54.74) | 1.93 (1.37-2.6) |
| ms | 150 | 12.78 (11.07-14.83) | 38.91 (35.98-42.15) | 1 | 4.45 | 0.511 | 68.87 (67.02-70.79) | 52.93 (50.49-55.53) | 2.35 (1.81-2.9) |
| th | 150 | 40.98 (38.13-43.97) | n/a | 0 | 7.83 | 1.273 | 59.77 (58.02-61.52) | 30.29 (28.62-32.17) | 7.77 (5.37-10.9) |
| vi | 150 | 26.64 (24.22-29.36) | 43.51 (40.45-46.97) | 0 | 5.21 | 0.84 | 62.55 (60.21-64.96) | 38.72 (36.49-41.17) | 4.4 (3.06-6.22) |
| tl | 150 | 15.93 (14.25-17.72) | 52.51 (49.62-55.68) | 0 | 4.3 | 0.304 | 70.55 (68.26-72.84) | 51.78 (49.43-53.93) | 4.56 (3.84-5.41) |
| km | 150 | 21.51 (19.68-23.56) | n/a | 0 | 5.17 | 0.396 | 61.82 (59.42-64.14) | 51.3 (49.14-53.48) | - |
| lo | 150 | 17.64 (14.79-20.76) | n/a | 0 | 4.52 | 0.422 | 61.76 (59.84-63.77) | 51.89 (49.69-54.22) | - |

The last column re-ran the six Whisper languages with Gemini transcribing instead (`--stt gemini`).

## Main finding: Gemini transcribes far better than Whisper `base`

| Language | Whisper base CER | Gemini CER | Errors cut by |
|---|---|---|---|
| Thai | 41.0% | 7.8% | 5.3x |
| Vietnamese | 26.6% | 4.4% | 6.0x |
| Filipino | 15.9% | 4.6% | 3.5x |
| Indonesian | 13.5% | 1.9% | 7.0x |
| Malay | 12.8% | 2.3% | 5.4x |
| English | 5.3% | 2.6% | 2.0x |

Gemini was also faster per clip (RTF about 0.3-0.4 against 0.3-1.3 for Whisper on the shared CPU). The relay
currently routes only Burmese, Khmer and Lao to Gemini, because Whisper cannot read them at all; these numbers
say the other languages would also be better served by it. Cost: one Gemini call per clip.

## How to read the columns
- **CER / WER**: character / word error rate after Unicode normalisation and punctuation removal, summed over
  all clips; 95% bootstrap intervals over clips (1,000 resamples, seed 0). A clip that produced no text counts as 100%.
  WER is n/a for Burmese, Thai, Khmer and Lao: those scripts do not separate words with spaces.
- **chrF++ from reference**: the human transcript translated into English, scored against the human English
  translation (isolates translation quality). **End to end**: the recognised text translated instead.
  chrF++ matches sacrebleu 2.x. The end-to-end drop (about 10-30 points) is mostly speech recognition.
- **Median s / RTF**: seconds to transcribe one clip, and processing time over audio time. Two Whisper containers
  shared one CPU, so Whisper's RTF is pessimistic (Thai above 1.0 would not keep up with live speech here).

## A bug this evaluation found
The first Gemini English run returned 7 empty clips (CER 6.2%). They were real, quiet speech with peaks of
0.002-0.0029 that the relay's own silence guard (peak below 0.003) had skipped before reaching Gemini. The guard now
uses 0.001 (`POLYGLOT_SILENCE_PEAK`); all 7 then transcribed (7.7% CER on those clips) and English Gemini became 2.6%.
Exactly those 7 of the 1,350 clips were below the old threshold, so no other number here was affected.

## Limits of these numbers
- Each language was scored on its **own** first 150 clips, so sentence difficulty differs between languages;
  compare languages loosely. Read-aloud speech, not conversation; one run per setting.
- Only Whisper `base` was tried (the relay's default); larger Whisper sizes might narrow the gap.
- Earlier tests with synthesized speech gave 3-4% CER for Burmese. On real speakers it is 15.7%.
- Errors are spread across clips (median clip CER is close to the mean); output length matches the reference
  (ratio 0.94-1.01), except Thai with Whisper (32 of 150 clips above 50% CER) and Lao (8 clips above 50%).
- Thai via Gemini lost one 15 s window to a network handshake timeout, so 7.8% is slightly pessimistic.
- Per-clip outputs are not committed (about 4 MB); `eval.prepare_fleurs` and `eval.fleurs_eval` regenerate them.
