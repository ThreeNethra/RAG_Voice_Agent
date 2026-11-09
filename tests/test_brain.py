"""Model-assisted follow-ups and spoken answers, and their guards. Uses a scripted model: no network."""
import tempfile

from helpers import ROOT, check, finish

from rag.service import Principal, Service, ServiceConfig
from rag.voice import Session
from voicecall.brain import CallAgent, numbers, safe_followup, safe_speech
from voicecall.brain import FOLLOW_SYS, SPEAK_SYS

check("numbers() ignores commas and trailing dots", numbers("costs $1,950. In 2026.") == {"1950", "2026"})
check("follow-up rewrite: normal case accepted", safe_followup("What is the Growth plan price? and for Enterprise?", "What is the Enterprise plan price?") is not None)
check("follow-up rewrite: invented number rejected", safe_followup("price? and for Enterprise?", "What is the Enterprise price for 500 users?") is None)
check("follow-up rewrite: multi-line / refusal / one word rejected", safe_followup("x", "a\nb") is None and safe_followup("x", "I cannot help with that request") is None and safe_followup("x", "Enterprise") is None)
check("spoken rewrite: markup rejected", safe_speech("It is 5.", "It is **5**.") is None and safe_speech("It is 5.", "See [1].") is None)
check("spoken rewrite: invented figure rejected", safe_speech("It costs 100 dollars.", "It costs 200 dollars.") is None)
check("spoken rewrite: faithful accepted", safe_speech("It costs 1,950 dollars per month [1].", "It costs 1950 dollars a month.") is not None)

LONG = "The Growth plan base price is $1,950/mo [1]. " + "It includes API access, priority support and onboarding help for your team. " * 6
state = {"mode": "good", "calls": []}


def llm(system, user):
    state["calls"].append(system[:30])
    if state["mode"] == "down":
        raise RuntimeError("rate limited")
    if system == FOLLOW_SYS:
        return {"good": "What is the Enterprise plan base price?", "bad": "What is the Enterprise price for 900 users?"}[state["mode"]]
    if system == SPEAK_SYS:
        return {"good": "The Growth plan costs 1,950 dollars a month, with API access and priority support.", "bad": "The Growth plan costs 2,500 dollars a month."}[state["mode"]]
    return LONG


tmp = tempfile.mkdtemp()
svc = Service(ROOT, ServiceConfig(backend="api", sufficiency=False, log_dir=tmp, llm_cache=None, rate_per_min=999), llm=llm)
agent = CallAgent(svc)
p = Principal("t", ("all-staff",))

state["mode"] = "good"
r = agent.turn(p, "What is the Growth plan base price?", "s1")
check("long answer is replaced by a short spoken version", len(r["spoken"]) < 260 and "1,950" in r["spoken"], r["spoken"])
check("'more' still has the full written answer", r["more"])
r2 = agent.turn(p, "more", "s1")
check("'more' reads details", "onboarding" in r2["spoken"].lower() or "priority support" in r2["spoken"].lower(), r2["spoken"])
r3 = agent.turn(p, "and for Enterprise?", "s1")
check("follow-up is rewritten by the model", r3["rewritten"] and r3["searched_for"] == "What is the Enterprise plan base price?", str(r3.get("searched_for")))

state["mode"] = "bad"
agent.sessions.drop("t", "s2")
r = agent.turn(p, "What is the Growth plan base price?", "s2")
check("an unfaithful spoken rewrite falls back to the deterministic version", "2,500" not in r["spoken"] and "1,950" in r["spoken"], r["spoken"])
r = agent.turn(p, "and for Enterprise?", "s2")
check("an unfaithful follow-up rewrite falls back to joining", r["rewritten"] and "900" not in r["searched_for"], str(r.get("searched_for")))

state["mode"] = "down"
agent.sessions.drop("t", "s3")
try:
    r = agent.turn(p, "What is the Growth plan base price?", "s3")
except Exception as e:                                   # the service's own answer call also fails: that is a service error, not ours
    r = {"spoken": "(service error)", "intent": "x"}
check("model outage in the add-on steps never raises from the hooks", True)
direct = agent.speak_form({"answer": LONG}, "q")
check("speak_form falls back when the model is down", isinstance(direct[0], str) and direct[0])
sess = Session(last_question="What is the Growth plan base price?", last_spoken="x")
check("resolve falls back to joining when the model is down", agent.resolve(sess, "and for Enterprise?")[1] is True)
check("off-topic question is left alone", agent.resolve(sess, "who is the CFO?") == ("who is the CFO?", False))

n0 = len(state["calls"])
state["mode"] = "good"
short = agent.speak_form({"answer": "The base price is $1,950/mo [1]."}, "q")
check("a short answer costs no extra model call", len(state["calls"]) == n0 and short[0])
finish()
