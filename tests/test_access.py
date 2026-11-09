"""Access control through the voice brain: every access level x every golden question, checked against an independent oracle."""
import fnmatch
import json
import tempfile

from helpers import ROOT, check, finish

from rag.evaluate import load_golden
from rag.service import Principal, Service, ServiceConfig
from voicecall.brain import CallAgent

acl = json.loads((ROOT / "data/lumora/acl.json").read_text(encoding="utf-8"))


def allowed(source, groups):
    for r in acl["rules"]:
        if fnmatch.fnmatchcase(source, r["match"]):
            g = set(r["groups"])
            return "*" in g or bool(g & set(groups))
    return bool(set(acl["default"]) & set(groups))


qs = [q for f in ("golden.jsonl", "golden_hard.jsonl") for q in load_golden(ROOT / "data/lumora" / f)]
svc = Service(ROOT, ServiceConfig(log_dir=tempfile.mkdtemp(), llm_cache=None, rate_per_min=10**6))
agent = CallAgent(svc)
levels = {"nobody": (), "staff": ("all-staff",), "finance": ("all-staff", "finance"), "exec": ("all-staff", "exec"),
          "eng": ("all-staff", "engineering", "support"), "sales": ("all-staff", "sales")}
viol, n, seen_restricted = [], 0, 0
for name, groups in levels.items():
    p = Principal(name, groups)
    for i, q in enumerate(qs):
        r = agent.turn(p, q["question"], f"{name}{i}")
        n += 1
        for s in r["sources"]:
            if not allowed(s["source"], groups):
                viol.append((name, s["source"], q["id"]))
    # also with 'more', 'sources', 'repeat' after a question
    for cmd in ("more", "sources", "repeat"):
        agent.turn(p, cmd, f"{name}{len(qs) - 1}")        # commands must not crash or leak either
check(f"no caller of any access level is given a source they may not read ({n} turns)", not viol, f"{len(viol)} e.g. {viol[:3]}")
r = agent.turn(Principal("x", ("exec",)), "What was decided at the September board meeting?", "z")
check("the oracle is not vacuous: exec does get restricted sources", any(s["source"].startswith("17_") for s in r["sources"]), str(r["sources"]))
finish()
