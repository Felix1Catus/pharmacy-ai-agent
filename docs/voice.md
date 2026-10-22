# Voice notes (Sprint C) — Gulf-Arabic speech → the same agent

Customers can send **voice notes** on WhatsApp (and in the test chat). The note is turned into
text on the server's CPU and handled exactly like a typed message — menu, customer
recognition, safety guards, search, orders, prescriptions. The reply starts with what was
understood, so the customer can correct a mishearing:

> 🎤 «عندكم فيتامين سي؟ وكم سعره؟»
> نعم، فيتامين سي 1000 ملغ متوفر بسعر …

## Pipeline (`adapter/voice.py`)
1. **Decode** any audio (WhatsApp Opus `.ogg`, browser `.webm`, iPhone `.m4a`, `.mp3`, `.wav`)
   with ffmpeg → 16 kHz mono, at most `ASR_MAX_SECONDS` (120 s; longer notes are cut and the
   reply says so).
2. **Language:** Arabic, unless Whisper is ≥70% sure it is English (short Gulf clips are
   otherwise sometimes detected as Persian/Urdu).
3. **Speech-to-text:** faster-whisper on CPU (int8), model `ASR_MODEL` (default `small`), with a
   **Gulf pharmacy prompt** (ابغى، وش، عندكم، علبتين، بنادول، توصيل، فرع، الرياض …) that biases
   Whisper towards the words customers use.
4. **Repairs:** pharmacy words Whisper splits are rejoined («في تامين» → «فيتامين»); runs of
   spoken digits become numbers («صفر خمسة واحد …» → `051…`, also common mishearings such as
   «وافد» for واحد) so phone numbers and order numbers work — including in registration.
5. The transcript goes through `PharmacyAgent.respond()`; an empty/unclear note gets
   «ما قدرت أسمع الرسالة الصوتية بوضوح، ممكن تعيدها …».

## Model choice (measured on this 4-vCPU server, `results/asr_bench.json`)
24 synthetic Saudi-voice notes (12 prompts × male/female):

| Model | Prompt | Seconds per note | Character errors | Drug name right |
|---|---|---|---|---|
| small | plain | 2.1 | 17.1% | 73% |
| **small** | **Gulf prompt** | **2.4** | **7.9%** | **91%** |
| large-v3-turbo | plain | 6.6 | 15.8% | 86% |
| large-v3-turbo | Gulf prompt | 6.9 | 12.1%* | 91% |

\* turbo writes phone numbers as digits while the reference uses words, which inflates its
error rate. It is 3× slower and no better on drug names, so `small` is the default; switch with
`ASR_MODEL=large-v3-turbo` in `.env` (e.g. on a bigger server).

**Synthetic audio is a plumbing test and first signal, not accuracy evidence.** Real customer
voice notes (noise, speed, real Gulf speakers) are needed — see `evals/datasets/recording_script.md`.

## Channels
- **WhatsApp:** `audio` messages (voice notes) are downloaded from Meta and transcribed
  (`channels/whatsapp/webhook.py`).
- **Test chat:** 🎤 records in the browser (tap to start, tap again to send). Browsers only allow
  the microphone on **https**: use **https://155.138.193.148:8443** (see below). On the plain-http
  page (:8090) attach an audio file with 📎 instead. The debug line shows the transcript and how
  long speech-to-text took.

### https for the test chat (Caddy, self-signed)
Caddy (`/etc/caddy/Caddyfile`, systemd `caddy`) serves **https://155.138.193.148:8443** with a
certificate from its own local authority (`tls internal`) and forwards to the chat on :8090
(same login). Because the certificate is self-signed the browser warns once: *Advanced → Proceed
to 155.138.193.148*, then allow the microphone. Firewall: `ufw allow 8443/tcp`.
With a domain: replace the site address with the domain and remove `tls internal` — Caddy then
gets a real Let's Encrypt certificate automatically (also the way to put Odoo / the WhatsApp
webhook behind https).

## Evaluate
```bash
.venv/bin/python evals/datasets/make_synthetic_voice.py      # Saudi TTS voices (edge-tts), synthetic
.venv/bin/python evals/runners/asr_bench.py                   # speech-to-text speed + accuracy
.venv/bin/python evals/runners/voice_eval.py                  # voice note -> agent action vs typed
.venv/bin/python -m pytest adapter/test_voice.py -q
```
For real recordings, put them under `evals/datasets/audio/` as named in `voice_manifest.json` and
run the same scripts with `--manifest`.

## Next
- Real Gulf voice notes from the owner/staff (50+) → measure, then decide on a dialect-tuned
  Whisper if `small` misses too much.
- Spoken replies (text-to-speech voice answers) — later.
