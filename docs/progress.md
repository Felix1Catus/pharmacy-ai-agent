# Progress log

> Keep this short. Update at the end of each task/sprint: what was done, the numbers,
> what is blocked on the human, what you recommend next. Nothing is "done" unless its
> acceptance check ran and its result file exists.

## BLOCKED (needs the human — see docs/blockers for detail)
- **Target dialect(s)** — assuming Saudi/Gulf Arabic + English until confirmed. [A]
- **Real voice notes + transcripts** (50+, noisy/quiet, numbers, drug/brand names). [A, C]
- **Native-speaker review** of dialect text + fine-tuning dataset. [A, D]
- **Product list sample** (Arabic + English) — using a machine-drafted pharmacy set until provided. [A]
- **Meta WhatsApp Cloud API** test-number credentials. [B]
- **GPU server (L40S 48 GB)** started + SSH access — Whisper/Qwen3/TTS/fine-tune all wait on this. [A onward]

All GPU-dependent benchmarks (T-A5 Whisper, T-A6 TTS, T-A7 Qwen3) are blocked on the GPU box.
Proceeding with everything that does not depend on the above.

---

## 2026-10-16 — Environment & Sprint A kickoff (new workstation)
- Rebuilt the project fresh on a new Vultr workstation (32 vCPU / 125 GB RAM, Ubuntu 24.04).
  Previous workstation was 4 vCPU / 7.7 GB. GPU work still offloaded to the L40S box over SSH.
- Installed: ffmpeg, python3.12-venv, Docker engine 29.9.1 + compose plugin.
- **Scope change (human-approved):** replaced the planned *mock* Odoo with a **real Odoo 18**
  instance in Docker, fronted by a FastAPI contract adapter. See docs/decisions.md.
