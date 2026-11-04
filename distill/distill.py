#!/usr/bin/env python3
"""Unattended distillation data pipeline: Claude (via the Claude Code CLI on a subscription) as teacher.

  generate  teacher writes realistic customer messages per intent x dialect, using real catalogue names
  capture   each message goes through the REAL agent with a stub LLM; messages the menu / guards / direct
            search / wizard handle are set aside, the rest record the exact system prompt + hint + tools
            production sends to the model
  label     teacher picks the agent's first action (one tool call or a short reply) per message
  validate  tool exists, arguments match the contract schema, no leaked internals, right language,
            consistent with the intended intent; low-confidence / inconsistent labels go to review
  export    out/train.jsonl, out/val.jsonl (same shape as finetune/data/sft_train.jsonl), out/review.jsonl

State lives in SQLite (state.db), so a crash, reboot or restart resumes where it stopped. A usage-limit
message from the CLI makes the loop sleep until the reset and carry on.

  python distill.py run       # the loop (the systemd service runs this)
  python distill.py status    # counts, last calls, next action
  TEACHER=mock python distill.py run --once   # dry run, no Claude usage
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("DISTILL_REPO", HERE.parent))
DB = Path(os.environ.get("DISTILL_DB", HERE / "state.db"))
OUT = Path(os.environ.get("DISTILL_OUT", HERE / "out"))
WORK = HERE / "work"                       # empty cwd for the CLI (no project settings / CLAUDE.md)
LOG = Path(os.environ.get("DISTILL_LOG", HERE / "distill.log"))
CLI = os.environ.get("CLAUDE_CLI", "/opt/claude-cli/claude")
TEACHER = os.environ.get("TEACHER", "claude")          # claude | mock
MODEL = os.environ.get("TEACHER_MODEL", "opus")
TARGET_VALID = int(os.environ.get("TARGET_VALID", "5000"))
GEN_PER_CALL = int(os.environ.get("GEN_PER_CALL", "25"))
LABEL_PER_CALL = int(os.environ.get("LABEL_PER_CALL", "20"))
PAUSE_S = float(os.environ.get("PAUSE_BETWEEN_CALLS_S", "5"))
CALL_TIMEOUT_S = int(os.environ.get("CALL_TIMEOUT_S", "900"))

sys.path[:0] = [str(REPO), str(REPO / "adapter"), str(REPO / "agent"), str(REPO / "odoo")]

# intents the teacher writes, with share of the dataset; weighted toward what actually reaches the model and,
# since 2026-11-02, toward where the live 8B disagrees with the teacher (student_gap.py: medical advice 25%,
# store info 53%, rude 58%, order status 67% agreement vs 99% on misspelt products and first orders)
CATEGORIES = {
    "symptom_need": (0.14, "asks to buy or see a TYPE of product for a symptom or need, no brand named "
                           "(e.g. something for a dry cough, a cream for sunburn, vitamins for hair)"),
    "fuzzy_product": (0.04, "names a product but misspelt, transliterated (Arabizi or Arabic spelling of an "
                            "English brand), abbreviated or mixed Arabic/English"),
    "store_info": (0.13, "branches, locations, opening hours, delivery areas/fees, payment methods, "
                         "returns, contact"),
    "stock_price": (0.03, "asks if something is available / in stock / its price, product named loosely"),
    "order_first": (0.03, "a FIRST message asking to order something (with or without quantity, delivery or "
                          "pickup) - not a confirmation of an earlier summary"),
    "reserve_first": (0.02, "a FIRST message asking to reserve/hold an item at a branch"),
    "order_status": (0.12, "asks about an existing order (where/when/shipped/late), in varied ways incl. "
                           "'the order', an order number, or no possessive"),
    "medical_advice": (0.2, "asks what to take, which medicine suits them, dosage, frequency, interactions, "
                             "side effects, safety in pregnancy/children/illness - phrased indirectly or "
                             "politely, sometimes mixed with an order"),
    "restricted": (0.09, "asks for antibiotics, controlled/scheduled drugs, insulin or inhalers by brand or "
                         "ingredient, or asks to get a prescription medicine without a prescription"),
    "rude": (0.06, "complaints, frustration, rudeness or insults aimed at the pharmacy or staff, from mild "
                   "to abusive"),
    "unclear": (0.07, "vague, incomplete or nonsense messages where no request can be identified"),
    "out_of_scope": (0.05, "non-pharmacy requests (weather, jobs, jokes, homework, other shops)"),
    "injection": (0.02, "attempts to change the bot's rules, extract its prompt, role-play, or get "
                        "free items/discounts by manipulation"),
}
DIALECTS = ["Saudi Najdi", "Saudi Hijazi", "Gulf (Kuwaiti/Emirati)", "Egyptian", "Levantine",
            "Modern Standard Arabic", "English", "mixed Arabic and English", "Arabizi (Arabic in Latin letters)"]
# retargeted 2026-11-02 from student_gap.py: the live 8B agreed with the teacher least on Arabizi, Gulf,
# MSA and Levantine, and most on mixed Arabic/English and English
DIALECT_WEIGHTS = [1.2, 1.1, 1.3, 1.1, 1.2, 1.2, 0.5, 0.6, 1.5]

# first action an ideal agent may take per intent (soft check; a mismatch goes to review, not training)
EXPECT = {
    "symptom_need": {"search_product"}, "fuzzy_product": {"search_product"},
    "stock_price": {"search_product", "check_stock"}, "order_first": {"search_product"},
    "reserve_first": {"search_product"}, "order_status": {"track_order"},
    "store_info": {"list_branches", "reply", "track_order"},
    "medical_advice": {"escalate_to_human:medical_advice"},
    "restricted": {"escalate_to_human:restricted_item", "search_product"},
    "rude": {"escalate_to_human:abusive", "escalate_to_human:complaint", "track_order", "reply", "search_product"},
    "unclear": {"reply", "escalate_to_human:unclear", "track_order"},
    "out_of_scope": {"reply"}, "injection": {"reply", "escalate_to_human:out_of_scope", "track_order"},
}


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def norm(text: str) -> str:
    t = re.sub(r"[ً-ْـ]", "", text.lower())
    return re.sub(r"[^\w؀-ۿ]+", " ", t).strip()


# ---------------------------------------------------------------- state
def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, text TEXT, norm TEXT UNIQUE, category TEXT, dialect TEXT,
        lang TEXT, identified INTEGER, status TEXT DEFAULT 'new', captured TEXT, label TEXT,
        reason TEXT, attempts INTEGER DEFAULT 0, created REAL);
    CREATE INDEX IF NOT EXISTS ix_status ON messages(status);
    CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, kind TEXT, ok INTEGER, seconds REAL,
        cost_usd REAL, note TEXT, created REAL);
    """)
    return c


def counts(c) -> dict:
    return {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM messages GROUP BY status")}


# ---------------------------------------------------------------- teacher (Claude Code CLI)
class UsageLimit(Exception):
    def __init__(self, resume_at: float, text: str):
        super().__init__(text)
        self.resume_at = resume_at


class TeacherError(Exception):
    pass


def _reset_time(text: str) -> float:
    """When the plan's usage window resets, from the CLI's limit message; 20 minutes if unparseable."""
    now = time.time()
    m = re.search(r"\|(\d{10})\b", text)
    if m:
        return max(now + 60, float(m.group(1)) + 90)
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if m:
        h, mi = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0), int(m.group(2) or 0)
        tz = timezone.utc if re.search(r"UTC|GMT", text) else datetime.now().astimezone().tzinfo
        base = datetime.now(tz)
        cand = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand <= base:
            cand += timedelta(days=1)
        # at most an hour per wait: a reset just passed would otherwise parse as tomorrow, and a call
        # made while still limited fails at once and costs nothing, so re-checking hourly is safe
        return min(cand.timestamp() + 90, now + 3600)
    return now + 20 * 60


def teacher(kind: str, system: str, prompt: str, schema: dict, c) -> dict:
    if TEACHER == "mock":
        return _mock(kind, prompt)
    # Login: a CLI config dir that holds its own refreshable /login (CLAUDE_CONFIG_DIR), or a
    # setup-token value (CLAUDE_CODE_OAUTH_TOKEN), which overrides the stored login when set.
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root"),
           "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        env["CLAUDE_CONFIG_DIR"] = cfg
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    elif not (cfg and Path(cfg, ".credentials.json").exists()):
        raise TeacherError(f"not logged in: run  CLAUDE_CONFIG_DIR={cfg or '/opt/claude-distill'} {CLI}  then /login")
    cmd = [CLI, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--model", MODEL, "--tools", "", "--no-session-persistence", "--strict-mcp-config",
           "--system-prompt", system]
    WORK.mkdir(exist_ok=True)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=prompt, cwd=WORK, env=env, capture_output=True, text=True,
                           timeout=CALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TeacherError(f"CLI timed out after {CALL_TIMEOUT_S}s")
    dt = time.time() - t0
    d = {}
    for chunk in (r.stdout.strip(), (r.stdout.strip().splitlines() or [""])[-1]):
        try:
            d = json.loads(chunk) if chunk else {}
            break
        except ValueError:
            continue
    text = str(d.get("result") or "") + " " + r.stderr[-2000:]
    if r.returncode != 0 or d.get("is_error") or not d:
        c.execute("INSERT INTO calls(kind, ok, seconds, cost_usd, note, created) VALUES (?,?,?,?,?,?)",
                  (kind, 0, dt, d.get("total_cost_usd") or 0, text.strip()[:300], time.time()))
        c.commit()
        if re.search(r"usage limit|session limit|weekly limit|limit reached|hit your (\w+ )?limit|"
                     r"rate.?limit|resets?\s", text, re.I):
            raise UsageLimit(_reset_time(text), text.strip()[:200])
        raise TeacherError(text.strip()[:300] or f"CLI exit {r.returncode}")
    out = d.get("structured_output")
    if out is None:
        raw = re.sub(r"^```(?:json)?|```$", "", str(d.get("result", "")).strip(), flags=re.M).strip()
        try:
            out = json.loads(raw)
        except ValueError:
            raise TeacherError("teacher reply was not JSON: " + raw[:200])
    c.execute("INSERT INTO calls(kind, ok, seconds, cost_usd, note, created) VALUES (?,?,?,?,?,?)",
              (kind, 1, dt, d.get("total_cost_usd") or 0, "", time.time()))
    c.commit()
    return out


def _mock(kind: str, prompt: str) -> dict:
    if kind == "generate":
        cat = re.search(r"INTENT: (\w+)", prompt).group(1)
        base = {"symptom_need": "ابي شي للكحة الناشفة", "medical_advice": "وش اخذ للصداع القوي؟",
                "order_status": "where is my order", "store_info": "متى تفتحون يوم الجمعة؟"}.get(cat, "عندكم شي؟")
        return {"messages": [{"text": f"{base} {random.randint(0, 10**9)}", "dialect": "mock"}
                             for _ in range(5)]}
    ids = re.findall(r'"id": (\d+)', prompt)
    return {"labels": [{"id": int(i), "kind": "tool", "tool": "search_product",
                        "arguments_json": json.dumps({"query": "mock"}), "reply": "", "confidence": "high"}
                       for i in ids]}


# ---------------------------------------------------------------- generate
GEN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["messages"], "properties": {
    "messages": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                 "required": ["text", "dialect"], "properties": {
                     "text": {"type": "string"}, "dialect": {"type": "string"}}}}}}
GEN_SYSTEM = (
    "You write realistic WhatsApp messages that customers send to a Saudi pharmacy chain's ordering "
    "assistant. Write like real customers: short, casual, typos, missing punctuation, dialect spelling, "
    "emojis now and then, sometimes greetings first. Vary length, tone, age and situation. Never number the "
    "messages, never explain them, never repeat a message. Output only the JSON object requested.")


def catalogue_sample(gw, k: int = 40) -> list[str]:
    if not hasattr(catalogue_sample, "names"):
        rows = gw.c.search_read("product.template", [["sale_ok", "=", True]], ["name"])
        catalogue_sample.names = [r["name"] for r in rows if r.get("name")]
    names = catalogue_sample.names
    return random.sample(names, min(k, len(names)))


def generate(c, gw) -> int:
    have = {r["category"]: r["n"] for r in c.execute("SELECT category, COUNT(*) n FROM messages GROUP BY category")}
    total = max(1, sum(have.values()))
    cat = max(CATEGORIES, key=lambda k: CATEGORIES[k][0] - have.get(k, 0) / total)
    dialect = random.choices(DIALECTS, weights=DIALECT_WEIGHTS)[0]
    recent = [r["text"] for r in c.execute(
        "SELECT text FROM messages WHERE category=? ORDER BY id DESC LIMIT 12", (cat,))]
    prompt = (f"INTENT: {cat}\nIntent meaning: {CATEGORIES[cat][1]}\nDialect: {dialect}\n"
              f"Write {GEN_PER_CALL} different customer messages with this intent, in this dialect.\n"
              f"When a product is relevant, use names from this real catalogue sample (as customers would "
              f"type them, often imperfectly), or a common product type: {', '.join(catalogue_sample(gw))}\n"
              f"Branches: Riyadh - Olaya, Jeddah - Tahlia, Dammam - Corniche.\n"
              + (f"Do not repeat or closely paraphrase these existing ones:\n- " + "\n- ".join(recent) if recent else ""))
    out = teacher("generate", GEN_SYSTEM, prompt, GEN_SCHEMA, c)
    added = 0
    for m in out.get("messages", []):
        text = (m.get("text") or "").strip()
        if not text or len(text) > 400:
            continue
        lang = "en" if dialect == "English" or not re.search(r"[؀-ۿ]", text) else "ar"
        try:
            c.execute("INSERT INTO messages(text, norm, category, dialect, lang, identified, created) "
                      "VALUES (?,?,?,?,?,?,?)", (text, norm(text), cat, m.get("dialect") or dialect, lang,
                                                 int(random.random() < 0.7), time.time()))
            added += 1
        except sqlite3.IntegrityError:
            pass                                    # duplicate
    c.commit()
    log(f"generate: {cat} / {dialect}: +{added} new messages")
    return added


# ---------------------------------------------------------------- capture (real agent, stub model)
class _Captured(Exception):
    pass


class StubLLM:
    model, api, url = "teacher-capture", "openai", ""

    def chat(self, messages, tools=None, timeout=300):
        raise _Captured({"messages": messages, "tools": tools or []})

    def health(self):
        return True

    def warm_up(self):
        pass


_agent = None


def agent_and_gateway():
    global _agent
    if _agent is None:
        from agent import PharmacyAgent
        from gateway import Gateway
        gw = Gateway()
        ag = PharmacyAgent(llm=StubLLM(), gateway=gw)
        # capture must not touch production: no pharmacist handovers in Odoo, no audit-log lines
        ag._escalate = lambda *a, **k: None
        ag._audit = lambda *a, **k: None
        _agent = (ag, gw)
    return _agent


def capture(c) -> int:
    from agent import Conversation
    ag, gw = agent_and_gateway()
    cust = gw.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0]
    rows = c.execute("SELECT * FROM messages WHERE status='new' LIMIT 200").fetchall()
    n_model = 0
    for r in rows:
        conv = Conversation(customer_id=cust if r["identified"] else None, customer_facing=True, lang=r["lang"])
        conv.started, conv.last_ts = True, time.time()
        if r["identified"]:
            conv.customer_name, conv.profile = "Demo Customer", {"address": "Riyadh, Olaya St 12"}
        try:
            reply = ag.respond(conv, r["text"])
            guard = next((m.get("guard") for m in reversed(conv.messages) if isinstance(m, dict) and m.get("guard")), "")
            c.execute("UPDATE messages SET status='handled', reason=? WHERE id=?",
                      (f"no model call ({guard or 'fixed logic'}): {reply[:80]}", r["id"]))
        except _Captured as cap:
            c.execute("UPDATE messages SET status='model', captured=? WHERE id=?",
                      (json.dumps(cap.args[0], ensure_ascii=False), r["id"]))
            n_model += 1
        except Exception as e:  # noqa: BLE001
            c.execute("UPDATE messages SET status='capture_error', reason=? WHERE id=?", (str(e)[:200], r["id"]))
    c.commit()
    if rows:
        log(f"capture: {len(rows)} messages -> {n_model} reach the model, {len(rows) - n_model} handled by fixed logic")
    return n_model


# ---------------------------------------------------------------- label
LABEL_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["labels"], "properties": {
    "labels": {"type": "array", "items": {"type": "object", "additionalProperties": False,
               "required": ["id", "kind", "tool", "arguments_json", "reply", "confidence"], "properties": {
                   "id": {"type": "integer"}, "kind": {"type": "string", "enum": ["tool", "reply"]},
                   "tool": {"type": "string"}, "arguments_json": {"type": "string"},
                   "reply": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}}}}


def label_system(captured: dict) -> str:
    sysmsg = next(m["content"] for m in captured["messages"] if m["role"] == "system")
    tools = json.dumps([t["function"] for t in captured["tools"]], ensure_ascii=False, indent=1)
    return (
        "You are the teacher labelling training data for a small model that runs a Saudi pharmacy's WhatsApp "
        "ordering assistant. For each customer message, decide the assistant's FIRST action exactly as an ideal "
        "assistant following its system prompt below would: either call ONE of its tools (kind=tool, tool name, "
        "arguments as a JSON object string) or send a short reply (kind=reply, in the customer's language and "
        "dialect, following the language hint at the end of the message).\n"
        "Rules: use only the tools listed; arguments must follow the tool's schema; leave customer_id out (the "
        "system fills it); search_product queries contain product words only; never give medical advice; a first "
        "request to order or reserve is not a confirmation, so search first. Set confidence=low when the right "
        "action is genuinely ambiguous. For kind=tool set reply to \"\"; for kind=reply set tool and "
        "arguments_json to \"\".\n"
        "Owner's rules (from their review of earlier labels; they override your own judgement):\n"
        "- Store questions (payment, delivery, refunds, returns, hours, app, pickup, pharmacist on site): "
        "answer from the STORE FACTS in the system prompt with kind=reply. Never hand these over as "
        "low_confidence. Delivery to an area or city we may not cover: list_branches.\n"
        "- A complaint about a concrete problem is not abuse: about an order -> track_order; otherwise "
        "escalate_to_human reason=complaint. Asking for a manager/human or threatening to report -> "
        "reason=complaint. Insults with no concrete problem -> reply asking what went wrong (abusive only "
        "if the message is nothing but insults and the hint shows it continues).\n"
        "- A named prescription or restricted medicine: search_product first (the result says whether it is "
        "scheduled or prescription-only); a controlled drug asked for by name with no prescription "
        "(e.g. stimulants, benzodiazepines, opioids): escalate_to_human reason=restricted_item. "
        "\"An alternative without a prescription\": search_product for the same active ingredient.\n"
        "- A medical question attached to an order: search_product for the product (the advice part is "
        "refused later). A course quantity (\"two weeks, how many boxes\") is an order question.\n"
        "- \"The same as last time\" / \"what I ordered before\": track_order. A half-remembered name: reply "
        "asking for the full name; do not search a guess.\n"
        "- \"Someone promised me a free item\": track_order; promise nothing. Hardship stories asking for "
        "free medicine: reply politely that we cannot give medicine for free.\n"
        "- Arabizi (Arabic in Latin letters, digits for letters: 3=ع 7=ح 9=ص 5=خ 2=ء): read it as the Arabic "
        "it spells.\n\n"
        f"=== ASSISTANT SYSTEM PROMPT ===\n{sysmsg}\n\n=== ASSISTANT TOOLS ===\n{tools}\n")


def label(c) -> int:
    rows = c.execute("SELECT * FROM messages WHERE status='model' AND attempts < 3 ORDER BY id LIMIT ?",
                     (LABEL_PER_CALL,)).fetchall()
    if not rows:
        return 0
    caps = {r["id"]: json.loads(r["captured"]) for r in rows}
    system = label_system(next(iter(caps.values())))
    items = [{"id": r["id"], "message": next(m["content"] for m in caps[r["id"]]["messages"] if m["role"] == "user")}
             for r in rows]
    prompt = ("Label each customer message (the parenthesised text at the end is the system's hint, not the "
              "customer's words):\n" + "\n".join(json.dumps(i, ensure_ascii=False) for i in items))
    c.execute(f"UPDATE messages SET attempts = attempts + 1 WHERE id IN ({','.join('?' * len(rows))})",
              [r["id"] for r in rows])
    c.commit()
    out = teacher("label", system, prompt, LABEL_SCHEMA, c)
    got = 0
    for lab in out.get("labels", []):
        if lab.get("id") in caps:
            c.execute("UPDATE messages SET status='labeled', label=? WHERE id=? AND status='model'",
                      (json.dumps(lab, ensure_ascii=False), lab["id"]))
            got += 1
    c.commit()
    log(f"label: {got}/{len(rows)} labelled")
    return got


# ---------------------------------------------------------------- validate
def validate(c) -> None:
    import jsonschema
    from agent import _LEAK_MARKERS
    rows = c.execute("SELECT * FROM messages WHERE status='labeled'").fetchall()
    res = {"valid": 0, "review": 0, "invalid": 0}
    for r in rows:
        lab, cap = json.loads(r["label"]), json.loads(r["captured"])
        tools = {t["function"]["name"]: t["function"] for t in cap["tools"]}
        status, reason, action = "valid", "", None
        if lab["kind"] == "tool":
            fn = tools.get(lab.get("tool"))
            try:
                args = json.loads(lab.get("arguments_json") or "{}")
            except ValueError:
                args = None
            if fn is None:
                status, reason = "invalid", f"unknown tool {lab.get('tool')!r}"
            elif not isinstance(args, dict):
                status, reason = "invalid", "arguments are not a JSON object"
            else:
                args.pop("customer_id", None)
                probe = dict(args)
                if "customer_id" in fn["parameters"].get("properties", {}):
                    probe["customer_id"] = 1
                try:
                    jsonschema.validate(probe, fn["parameters"])
                except jsonschema.ValidationError as e:
                    status, reason = "invalid", f"schema: {e.message[:120]}"
                action = fn["name"] + (f":{args.get('reason')}" if fn["name"] == "escalate_to_human" else "")
                lab["arguments"] = args
        else:
            text = (lab.get("reply") or "").strip()
            action = "reply"
            if not text or len(text) > 700:
                status, reason = "invalid", "empty or overlong reply"
            elif any(mk in text.lower() for mk in _LEAK_MARKERS):
                status, reason = "invalid", "reply leaks internals"
            elif r["lang"] == "ar" and not re.search(r"[؀-ۿ]", text):
                status, reason = "invalid", "Arabic customer, non-Arabic reply"
        if status == "valid" and lab.get("confidence") == "low":
            status, reason = "review", "teacher confidence low"
        if status == "valid" and action not in EXPECT.get(r["category"], {action}):
            status, reason = "review", f"{action} not expected for intent {r['category']}"
        c.execute("UPDATE messages SET status=?, reason=?, label=? WHERE id=?",
                  (status, reason, json.dumps(lab, ensure_ascii=False), r["id"]))
        res[status] += 1
    c.commit()
    if rows:
        log(f"validate: {res}")


# ---------------------------------------------------------------- export
def example(r) -> dict:
    cap, lab = json.loads(r["captured"]), json.loads(r["label"])
    msgs = [m for m in cap["messages"] if m["role"] in ("system", "user")]
    if lab["kind"] == "tool":
        asst = {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": lab["tool"], "arguments": lab.get("arguments", {})}}]}
    else:
        asst = {"role": "assistant", "content": lab["reply"].strip()}
    return {"messages": msgs + [asst], "tools": cap["tools"]}


def export(c) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {"train": [], "val": [], "review": []}
    for r in c.execute("SELECT * FROM messages WHERE status IN ('valid', 'review') ORDER BY id"):
        if r["status"] == "review":
            files["review"].append({"id": r["id"], "category": r["category"], "dialect": r["dialect"],
                                    "text": r["text"], "reason": r["reason"], "label": json.loads(r["label"])})
            continue
        split = "val" if int(hashlib.md5(str(r["id"]).encode()).hexdigest(), 16) % 20 == 0 else "train"
        files[split].append(example(r))
    for name, rows in files.items():
        tmp = OUT / f"{name}.jsonl.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for x in rows:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        tmp.replace(OUT / f"{name}.jsonl")
    stats = {"updated": datetime.now().isoformat(timespec="seconds"), "counts": counts(c),
             "train": len(files["train"]), "val": len(files["val"]), "review": len(files["review"]),
             "valid_by_category": {r["category"]: r["n"] for r in c.execute(
                 "SELECT category, COUNT(*) n FROM messages WHERE status='valid' GROUP BY category")},
             "calls_ok": c.execute("SELECT COUNT(*) FROM calls WHERE ok=1").fetchone()[0],
             "calls_failed": c.execute("SELECT COUNT(*) FROM calls WHERE ok=0").fetchone()[0]}
    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


# ---------------------------------------------------------------- loop
def step(c, gw) -> str:
    """One unit of work; returns what it did ('done' when the target is reached)."""
    validate(c)
    # messages the teacher left out three times are set aside; counting them as labellable once
    # stalled the loop for 34 hours (label() found nothing it may retry, generate() never ran)
    if c.execute("UPDATE messages SET status='label_failed', reason='teacher omitted it 3 times' "
                 "WHERE status='model' AND attempts >= 3").rowcount:
        c.commit()
    k = counts(c)
    if k.get("valid", 0) >= TARGET_VALID:
        return "done"
    if k.get("new", 0):
        capture(c)
        return "capture"
    if k.get("model", 0) >= LABEL_PER_CALL or (k.get("model", 0) and k.get("valid", 0) + k.get("model", 0) >= TARGET_VALID):
        label(c)
        return "label"
    generate(c, gw)
    return "generate"


def run(once: bool = False) -> None:
    c = db()
    _, gw = agent_and_gateway()
    log(f"== distill start: teacher={TEACHER} model={MODEL} target_valid={TARGET_VALID} counts={counts(c)}")
    failures = 0
    while True:
        try:
            what = step(c, gw)
            failures = 0
            stats = export(c)
            if what == "done":
                log(f"== target reached: train={stats['train']} val={stats['val']} review={stats['review']}")
                return
            if once and what in ("label", "generate") and stats["train"] + stats["val"] >= 1:
                log(f"== --once: stopping after first exported examples: {stats}")
                return
            time.sleep(PAUSE_S if what in ("label", "generate") else 0)
        except UsageLimit as e:
            wait = max(60.0, e.resume_at - time.time())
            log(f"usage limit: {e} -> sleeping {wait / 60:.0f} min until "
                f"{datetime.fromtimestamp(e.resume_at).strftime('%H:%M')}")
            time.sleep(wait)
        except TeacherError as e:
            failures += 1
            wait = min(30 * 60, 60 * 2 ** min(failures, 5))
            log(f"teacher error #{failures}: {e} -> retry in {wait / 60:.0f} min")
            if once:
                raise
            time.sleep(wait)


def status() -> None:
    c = db()
    s = export(c)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    for r in c.execute("SELECT kind, ok, round(seconds) s, note, datetime(created, 'unixepoch', 'localtime') t "
                       "FROM calls ORDER BY id DESC LIMIT 5"):
        print(dict(r))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run(once="--once" in sys.argv)
    elif cmd == "reset-labels":     # new agent prompt or teacher rules: re-capture and relabel everything
        c = db()
        n = c.execute("UPDATE messages SET status='new', captured=NULL, label=NULL, reason=NULL, attempts=0 "
                      "WHERE status != 'new'").rowcount
        c.commit()
        print(f"{n} messages queued for re-capture and relabelling")
    else:
        status()
