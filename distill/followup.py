#!/usr/bin/env python3
"""Second-step distillation: what the assistant does AFTER a tool result comes back.

The first-step data (distill.py) only ever ends right after the customer's message, so a model trained on it
alone learns "always call a tool" and loops. In customer chats the model sees a tool result only after
track_order / list_branches (search results go to the fixed order wizard, a handover ends the turn), so:

  capture   for each valid first-step label that is track_order / list_branches, run the REAL agent with a
            scripted LLM: it returns the teacher's first call, the agent runs it against Odoo, and the next
            model request (system prompt + customer turn + tool call + tool result) is recorded exactly
  label     the teacher picks the next action (normally a reply built from the result)
  validate  same checks as the first step, plus: no repeat of the call just made
  export    out/followup_{train,val}.jsonl, split by the same message-id hash as the first step

State: table `followups` in state.db; usage limits are slept through like distill.py.
  python followup.py run | status
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time

import distill as d   # teacher(), UsageLimit, log, agent setup, the owner's rules  (also puts the agent on sys.path)

REPLAY = ("track_order", "list_branches")
PER_CALL = int(d.os.environ.get("FOLLOWUP_PER_CALL", "10"))


def db():
    c = d.db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS followups (
        id INTEGER PRIMARY KEY, status TEXT DEFAULT 'new', captured TEXT, label TEXT, reason TEXT,
        attempts INTEGER DEFAULT 0, created REAL);
    CREATE INDEX IF NOT EXISTS ix_fu_status ON followups(status);
    """)
    c.execute("""INSERT OR IGNORE INTO followups(id, created)
                 SELECT id, strftime('%s','now') FROM messages
                 WHERE status='valid' AND json_extract(label, '$.kind')='tool'
                   AND json_extract(label, '$.tool') IN ('track_order', 'list_branches')""")
    c.commit()
    return c


def counts(c) -> dict:
    return {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM followups GROUP BY status")}


# ---------------------------------------------------------------- capture
class ScriptedLLM(d.StubLLM):
    """First call: the teacher's label. Second call: record the request and stop."""

    def __init__(self, tool: str, args: dict):
        self.first = {"content": "", "tool_calls": [{"name": tool, "arguments": args}]}
        self.n = 0

    def chat(self, messages, tools=None, timeout=300):
        self.n += 1
        if self.n == 1:
            return self.first
        raise d._Captured({"messages": messages, "tools": tools or []})


def capture(c) -> int:
    from agent import Conversation
    ag, gw = d.agent_and_gateway()
    cust = gw.c.search("res.partner", [["ref", "=", "CUST-DEMO-1"]])[0]
    rows = c.execute("SELECT f.id, m.text, m.lang, m.identified, m.label FROM followups f JOIN messages m USING(id) "
                     "WHERE f.status='new' LIMIT 100").fetchall()
    got = 0
    for r in rows:
        lab = json.loads(r["label"])
        ag.llm = ScriptedLLM(lab["tool"], lab.get("arguments") or {})
        conv = Conversation(customer_id=cust if r["identified"] else None, customer_facing=True, lang=r["lang"])
        conv.started, conv.last_ts = True, time.time()
        if r["identified"]:
            conv.customer_name, conv.profile = "Demo Customer", {"address": "Riyadh, Olaya St 12"}
        try:
            reply = ag.respond(conv, r["text"])
            c.execute("UPDATE followups SET status='handled', reason=? WHERE id=?",
                      (f"no second model call: {reply[:80]}", r["id"]))
        except d._Captured as cap:
            msgs = cap.args[0]["messages"]
            if not any(m.get("role") == "tool" for m in msgs):      # the scripted call never ran (fixed logic)
                c.execute("UPDATE followups SET status='handled', reason='first call not a tool step' WHERE id=?",
                          (r["id"],))
            else:
                c.execute("UPDATE followups SET status='model', captured=? WHERE id=?",
                          (json.dumps(cap.args[0], ensure_ascii=False), r["id"]))
                got += 1
        except Exception as e:  # noqa: BLE001
            c.execute("UPDATE followups SET status='capture_error', reason=? WHERE id=?", (str(e)[:200], r["id"]))
    c.commit()
    if rows:
        d.log(f"followup capture: {len(rows)} -> {got} reach a second model call")
    return got


# ---------------------------------------------------------------- label
SECOND_STEP = (
    "\nTHIS BATCH IS DIFFERENT: each item is the assistant's SECOND step. The customer's message, the tool the "
    "assistant already called and the tool's result are shown. Decide the assistant's NEXT action:\n"
    "- Almost always kind=reply: a short WhatsApp reply in the customer's language and dialect that answers "
    "them from the tool result (order status / number / items, the branches and their areas). Use only facts "
    "in the result or the STORE FACTS; never invent dates, prices, stock or promises; never show internal ids, "
    "field names or JSON.\n"
    "- If the result shows no order for them, say so and offer to help them order; if their complaint is about "
    "an order and the result confirms a real problem (late, wrong, missing), escalate_to_human "
    "reason=complaint.\n"
    "- Call another tool only when the result clearly requires it; never repeat the call just made, and never "
    "create, reserve or change anything.\n")


def label(c) -> int:
    rows = c.execute("SELECT * FROM followups WHERE status='model' AND attempts < 3 ORDER BY id LIMIT ?",
                     (PER_CALL,)).fetchall()
    if not rows:
        return 0
    caps = {r["id"]: json.loads(r["captured"]) for r in rows}
    system = d.label_system(next(iter(caps.values()))) + SECOND_STEP
    items = []
    for r in rows:
        msgs = caps[r["id"]]["messages"]
        u = max(i for i, m in enumerate(msgs) if m["role"] == "user")
        trail = []
        for m in msgs[u:]:
            if m["role"] == "user":
                trail.append({"customer": m["content"]})
            elif m.get("tool_calls"):
                fn = m["tool_calls"][0]["function"]
                trail.append({"assistant_called": fn["name"], "arguments": fn.get("arguments")})
            elif m["role"] == "tool":
                trail.append({"tool_result": m["content"][:4000]})
        items.append({"id": r["id"], "conversation": trail})
    prompt = ("Decide the assistant's next action for each item (the parenthesised text at the end of the "
              "customer's message is the system's hint, not the customer's words):\n"
              + "\n".join(json.dumps(i, ensure_ascii=False) for i in items))
    c.execute(f"UPDATE followups SET attempts = attempts + 1 WHERE id IN ({','.join('?' * len(rows))})",
              [r["id"] for r in rows])
    c.commit()
    out = d.teacher("followup", system, prompt, d.LABEL_SCHEMA, c)
    got = 0
    for lab in out.get("labels", []):
        if lab.get("id") in caps:
            c.execute("UPDATE followups SET status='labeled', label=? WHERE id=? AND status='model'",
                      (json.dumps(lab, ensure_ascii=False), lab["id"]))
            got += 1
    c.commit()
    d.log(f"followup label: {got}/{len(rows)} labelled")
    return got


# ---------------------------------------------------------------- validate / export
def validate(c) -> None:
    import jsonschema
    from agent import _LEAK_MARKERS
    rows = c.execute("SELECT f.*, m.lang FROM followups f JOIN messages m USING(id) WHERE f.status='labeled'").fetchall()
    res = {"valid": 0, "review": 0, "invalid": 0}
    for r in rows:
        lab, cap = json.loads(r["label"]), json.loads(r["captured"])
        tools = {t["function"]["name"]: t["function"] for t in cap["tools"]}
        prev = next(m["tool_calls"][0]["function"] for m in reversed(cap["messages"]) if m.get("tool_calls"))
        status, reason = "valid", ""
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
                pa = {k: v for k, v in (prev.get("arguments") or {}).items() if k != "customer_id"}
                if status == "valid" and fn["name"] == prev["name"] and args == pa:
                    status, reason = "invalid", "repeats the call just made"
                lab["arguments"] = args
        else:
            text = (lab.get("reply") or "").strip()
            if not text or len(text) > 900:
                status, reason = "invalid", "empty or overlong reply"
            elif any(mk in text.lower() for mk in _LEAK_MARKERS):
                status, reason = "invalid", "reply leaks internals"
            elif re.search(r"\b(product_id|order_id|partner_id|branch_id)\b|[{}]", text):
                status, reason = "invalid", "reply shows ids or JSON"
            elif r["lang"] == "ar" and not re.search(r"[؀-ۿ]", text):
                status, reason = "invalid", "Arabic customer, non-Arabic reply"
        if status == "valid" and lab.get("confidence") == "low":
            status, reason = "review", "teacher confidence low"
        c.execute("UPDATE followups SET status=?, reason=?, label=? WHERE id=?",
                  (status, reason, json.dumps(lab, ensure_ascii=False), r["id"]))
        res[status] += 1
    c.commit()
    if rows:
        d.log(f"followup validate: {res}")


def example(r) -> dict:
    cap, lab = json.loads(r["captured"]), json.loads(r["label"])
    msgs = [m for m in cap["messages"] if m["role"] in ("system", "user", "assistant", "tool")]
    if lab["kind"] == "tool":
        asst = {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": lab["tool"], "arguments": lab.get("arguments", {})}}]}
    else:
        asst = {"role": "assistant", "content": lab["reply"].strip()}
    return {"messages": msgs + [asst], "tools": cap["tools"]}


def export(c) -> dict:
    files = {"train": [], "val": []}
    for r in c.execute("SELECT * FROM followups WHERE status='valid' ORDER BY id"):
        split = "val" if int(hashlib.md5(str(r["id"]).encode()).hexdigest(), 16) % 20 == 0 else "train"
        files[split].append(example(r))
    for name, rows in files.items():
        tmp = d.OUT / f"followup_{name}.jsonl.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for x in rows:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        tmp.replace(d.OUT / f"followup_{name}.jsonl")
    return {"counts": counts(c), "train": len(files["train"]), "val": len(files["val"])}


def run() -> None:
    c = db()
    d.agent_and_gateway()
    d.log(f"== followup start: {counts(c)}")
    failures = 0
    while True:
        try:
            validate(c)
            if c.execute("UPDATE followups SET status='label_failed', reason='teacher omitted it 3 times' "
                         "WHERE status='model' AND attempts >= 3").rowcount:
                c.commit()
            k = counts(c)
            if k.get("new"):
                capture(c)
            elif k.get("model"):
                label(c)
                failures = 0
                time.sleep(d.PAUSE_S)
            else:
                s = export(c)
                d.log(f"== followup done: {s}")
                return
        except d.UsageLimit as e:
            wait = max(60.0, e.resume_at - time.time())
            d.log(f"usage limit: {e} -> sleeping {wait / 60:.0f} min")
            time.sleep(wait)
        except d.TeacherError as e:
            failures += 1
            wait = min(30 * 60, 60 * 2 ** min(failures, 5))
            d.log(f"followup teacher error #{failures}: {e} -> retry in {wait / 60:.0f} min")
            time.sleep(wait)


if __name__ == "__main__":
    if (sys.argv[1:] or ["status"])[0] == "run":
        run()
    else:
        c = db()
        print(json.dumps(export(c), ensure_ascii=False, indent=1))
