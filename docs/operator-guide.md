# Operator guide — Qwen on this server: install layout, fine-tuning, and control

Server: Vultr, Ubuntu 24.04, 32 vCPU / 125 GB RAM, **no GPU**. Public IP `155.138.207.131`.

---

## 1. How Qwen sits on the server

There are **two copies of the same model**, each in the format its tool needs:

| | Serving copy (what the agent uses) | Training copy (what fine-tuning uses) |
|---|---|---|
| Format | **GGUF**, 4-bit quantized (`Q4_K_M`) | Hugging Face **safetensors**, full bf16 |
| Size | ~2.5 GB | ~7.6 GB |
| Lives in | `/usr/share/ollama/.ollama/models/` (managed by Ollama) | `/root/.cache/huggingface/hub/models--Qwen--Qwen3-4B-Instruct-2507/` |
| Used by | Ollama (llama.cpp engine) — fast CPU inference | PyTorch + transformers + PEFT — training |

Why two: inference is fastest on a compressed (quantized) GGUF; training needs the original
full-precision weights to compute gradients.

### The serving stack
```
customer msg ──> agent (Python, /home/pharmacy-ai-agent)
                   │  1. deterministic guards (agent/guards.py)  ← medical / scheduled blocks
                   │  2. LLM call over HTTP ──> Ollama (127.0.0.1:11434) ──> Qwen3-4B GGUF
                   │  3. tool calls ──> adapter/gateway.py ──> Odoo 18 (Docker, public :8069)
                   └─ reply
```
- **Ollama**: binary `/usr/local/bin/ollama`, runs as a systemd service `ollama` (user
  `ollama`), API on **127.0.0.1:11434** (localhost only — not exposed to the internet).
- **Model**: `hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M` — Qwen3, 4.02B params,
  non-thinking "Instruct-2507" build, tool-calling capable, Q4_K_M quantization.
- **Which model the agent uses**: `AGENT_MODEL` in `/home/pharmacy-ai-agent/.env`.
- **The agent's system instructions**: `SYSTEM_PROMPT` in `agent/agent.py` (sent with every
  request — see §3.3, this is the one that matters for customers).
- Python env for everything: `/home/pharmacy-ai-agent/.venv` (activate with
  `. .venv/bin/activate`).

---

## 2. How fine-tuning works, and how to do it yourself

### Concepts in one paragraph
Fine-tuning shows the model many *input → desired output* examples (here: customer message →
correct tool call) and nudges its weights toward producing them. **LoRA** freezes the original
model and trains small add-on "adapter" matrices (~1–2% of the parameters), so it's cheap and
the result is a small file. **QLoRA** = LoRA on a 4-bit-compressed base; faster/less memory but
**GPU-only** (bitsandbytes/unsloth need CUDA). On this CPU server we use plain LoRA.

### Tools
| Tool | Role |
|---|---|
| Python 3.12 venv | isolated environment |
| **PyTorch** (CPU build) | the math engine (forward/backward passes) |
| **Hugging Face transformers** | loads Qwen, chat template, Trainer loop (needs ≥4.51 for Qwen3; we use 5.x) |
| **PEFT** | adds/saves LoRA adapters |
| **datasets** | feeds examples to the trainer |
| **llama.cpp** (`/opt/llama.cpp-src`) | converts the trained model to GGUF and quantizes it |
| **Ollama** | serves the final GGUF to the agent |
| (GPU only) **unsloth + trl + bitsandbytes** | fast QLoRA — `finetune/train_qlora.py` |

### The pipeline, step by step (all scripts are in `finetune/`)
```bash
cd /home/pharmacy-ai-agent && . .venv/bin/activate

# 1. Build the training data (needs Odoo up; reads the real catalogue)
python finetune/mine_phrases.py        # Najdi dialect phrasing from the HF dataset
python finetune/build_sft.py           # -> finetune/data/sft_train.jsonl / sft_test.jsonl

# 2. Train a LoRA adapter (CPU). Long runs: ALWAYS detach (survives logout/session end)
FT_BASE=Qwen/Qwen3-4B-Instruct-2507 FT_OUT=results/ft_4b \
  setsid nohup python finetune/train_lora_cpu.py > /tmp/ft.log 2>&1 < /dev/null &
tail -f /tmp/ft.log                    # watch "step N/70 loss=..."; Ctrl-C only stops watching

# 3. Test it on the held-out set (base vs fine-tuned)
FT_BASE=Qwen/Qwen3-4B-Instruct-2507 FT_ADAPTER=results/ft_4b/adapter \
  python finetune/test_lora_cpu.py

# 4. Export to Ollama: merge adapter -> GGUF f16 -> quantize Q4_K_M -> ollama create
finetune/export_to_ollama.sh Qwen/Qwen3-4B-Instruct-2507 results/ft_4b/adapter pharmacy-4b-ft

# 5. Evaluate the full agent with it, BEFORE switching production
AGENT_MODEL=pharmacy-4b-ft python evals/runners/agent_eval.py

# 6. Only if it beats the baseline with no new wrong/dangerous actions: set in .env
#    AGENT_MODEL=pharmacy-4b-ft
```
Knobs in `train_lora_cpu.py`: `FT_BASE`, `FT_OUT`, `FT_MAX_STEPS` (default 70 ≈ 1 epoch),
learning rate 1e-4, LoRA r=16, max length 768 tokens. It checkpoints every 25 steps and
**auto-resumes** if you simply re-run the same command after an interruption.

### Lessons we learned the hard way (avoid these)
1. **Mask the prompt.** Score only the assistant's answer (`labels = -100` on the prompt). Our
   first run scored every token, learned to recite the system prompt, and got *worse*.
2. **Detach long jobs** (`setsid nohup …` or `tmux`). A run inside an SSH/terminal tab dies when
   the session ends — we lost a run at step 64 that way.
3. **Version pins matter**: transformers <4.51 doesn't know Qwen3; transformers 5 needs a recent
   `accelerate`, and `use_cpu=True` for bf16 on CPU.
4. **Ollama ≥0.40 no longer applies LoRA adapters at runtime** → merge first (the export script).
5. **Low training loss ≠ good model.** Always judge on the held-out test + the agent eval.
6. **Don't `pkill -f <script name>`** from a shell whose own command line contains that name — it
   kills itself. Use `pgrep -f "train_lora[_]cpu"` (the bracket trick).

---

## 3. Getting into the server and controlling Qwen via Ollama

### 3.1 Log in
```bash
ssh root@155.138.207.131
```
> ⚠️ Security: the server currently allows **root login with a password**
> (`PermitRootLogin yes`, `PasswordAuthentication yes`). Recommended: add your SSH key
> (`ssh-copy-id root@155.138.207.131`), confirm key login works, then set
> `PasswordAuthentication no` in `/etc/ssh/sshd_config.d/50-cloud-init.conf` and
> `systemctl restart ssh`. Not changed automatically — do it deliberately so you don't lock
> yourself out.

### 3.2 Everyday Ollama commands
```bash
ollama list                     # installed models
ollama ps                       # what's loaded in memory right now
ollama run hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M   # chat with it in the terminal
ollama show <model> --modelfile # see its template, parameters, system prompt
ollama stop <model>             # unload from memory
systemctl status ollama         # service health;  systemctl restart ollama  to restart
journalctl -u ollama -f         # live server log
```
Inside `ollama run` you can experiment live:
```
/set system "You are a pharmacy assistant. Reply in Saudi dialect. ..."
/set parameter temperature 0
/show system
/save pharmacy-test          # saves your session's system+params as a new model
/bye
```

### 3.3 Changing the system instructions — there are TWO layers
**(a) The agent's instructions (what customers actually get)** → edit `SYSTEM_PROMPT` in
`agent/agent.py`. The agent sends this as the `system` message on **every** request, which
**overrides** any SYSTEM baked into the Ollama model. So for the WhatsApp agent, this is the
only place that matters.
```bash
nano /home/pharmacy-ai-agent/agent/agent.py      # edit SYSTEM_PROMPT
cd /home/pharmacy-ai-agent && . .venv/bin/activate
python agent/chat.py "عندكم بنادول؟"              # quick manual check
python evals/runners/agent_eval.py               # full regression check before trusting it
```
The deterministic safety guards (`agent/guards.py`: medical advice, scheduled meds) run before
the model and **do not depend on the prompt** — prompt edits can't switch them off.

**(b) A model-level default (for direct Ollama use / other apps)** → make a derived model:
```
# file: Modelfile
FROM hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
SYSTEM """You are the ordering assistant for a pharmacy franchise ... (your text)"""
PARAMETER temperature 0
PARAMETER num_ctx 4096
```
```bash
ollama create pharmacy-assistant -f Modelfile
ollama run pharmacy-assistant
```
This adds a ~1 KB layer on top of the same weights (no extra 2.5 GB copy).

### 3.4 Calling the model directly (API)
```bash
curl -s http://localhost:11434/api/chat -d '{
  "model": "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M",
  "stream": false, "think": false, "options": {"temperature": 0},
  "messages": [
    {"role": "system", "content": "You are a pharmacy assistant."},
    {"role": "user",   "content": "عندكم بنادول؟"}
  ]}'
```

### 3.5 Odoo web access (public) and Ollama from your laptop
**Odoo** is reachable at **http://155.138.207.131:8069** — database `pharmacy`, login `admin`,
password = `ODOO_PASSWORD` in `/home/pharmacy-ai-agent/.env` (`grep ODOO_PASSWORD .env`).
Hardening applied when it was opened up:
- strong random **admin** password (the old `admin/admin` no longer works);
- strong random **master password** (`admin_passwd`, stored only in the gitignored
  `odoo/config/odoo.conf` and `ODOO_MASTER_PASSWORD` in `.env`; the repo has `odoo.conf.example`);
- **database manager disabled** (`list_db = False`) and `dbfilter = ^pharmacy$`;
- firewall rule `ufw allow 8069/tcp`.
> ⚠️ It is plain **HTTP**: passwords travel unencrypted. Before any real data or staff use, put
> HTTPS in front (a domain name + Caddy/nginx reverse proxy with Let's Encrypt), then bind Odoo
> back to localhost behind the proxy. To change the admin password: Odoo → Settings → Users →
> admin, then update `ODOO_PASSWORD` in `.env` (the agent logs in with it).
> To go back to local-only: in `odoo/docker-compose.yml` set `"127.0.0.1:8069:8069"`, then
> `cd odoo && docker compose up -d odoo` and `ufw delete allow 8069/tcp`.

**Ollama** stays localhost-only on purpose. From your laptop, tunnel it:
```bash
ssh -L 11434:localhost:11434 root@155.138.207.131
# then http://localhost:11434 on your laptop (scripts, Open WebUI, ...)
```

---

## 4. SFDA drugs registry → Odoo

Source: SFDA registered-drugs list (https://www.sfda.gov.sa/ar/drugs-list) + each drug's
التفاصيل page. Scraped pages 0–100 (the site's pager is 0-based, so this covers UI pages 1–101):
**2,020 entries → 2,014 unique drugs** (SFDA lists 6 twice).

Files in `data_sources/sfda/data/`:
| File | Use |
|---|---|
| `sfda_drugs_database.xlsx` / `.csv` | full reference database — every SFDA field, Arabic headers, + مقيد؟ / يحتاج وصفة؟ / source link |
| `sfda_products_odoo_import.xlsx` | **the file to import into Odoo** (also `.csv`, `_nobom.csv`) |
| `sfda_details.jsonl`, `sfda_list.jsonl` | raw scrape output |

Mapping: `المراقبة` = `مقيد` (45, e.g. pregabalin, alfentanil, bromazepam) **or**
`خاضعة للرقابة وفق أحكام وشروط خاصة` (30, e.g. somatropin, hCG, follitropin, dinoprostone) →
**Scheduled ✓ — 75 drugs** (owner decision);
`طريقة الصرف = يحتاج الى وصفة طبية` → *Prescription required (SFDA)* (1,768). The raw status is
kept in *Control status*, so the two categories can still be told apart (filter on it).
Everything else lands in the **Pharmacy tab → SFDA registry** section.

### Import (Odoo UI)
1. Inventory → Products → ⚙ (gear) → **Import records** → upload `sfda_products_odoo_import.xlsx`.
2. Columns auto-match (technical names). Click **Test** → expect no errors → **Import**.
3. Re-importing a newer file **updates** products (external ID `sfda.drug_<reg no>`), no duplicates.
Tested end-to-end on a scratch database (2,014 imported, 0 errors, 75 Scheduled).

> The gateway refuses orders/holds for SFDA prescription drugs (`sfda_requires_prescription`) as
> well as Scheduled ones, so the file is safe to import into the live DB. After importing, restart
> the agent so its scheduled-medicine block list reloads from Odoo.

### Refresh / extend
```bash
cd /home/pharmacy-ai-agent
.venv/bin/python data_sources/sfda/scrape_sfda.py 0 100     # resumable; SFDA_WORKERS=4 SFDA_DELAY=0.5
.venv/bin/python data_sources/sfda/build_odoo_import.py      # rebuild the files
```
More pages: change `0 100` (e.g. `0 455`); already-fetched pages are reused from the cache.
