"""Access control: who may retrieve which document.

THE RULE: the filter runs INSIDE retrieval, BEFORE ranking. Filtering after the answer is generated is
too late: the restricted text has already reached the model (and may be paraphrased into the answer
or the refusal message). Because every retrieval path in `Pipeline` goes through `Retriever.search`,
one check there covers dense, sparse, hybrid, query expansion, decomposition, HyDE, iterative
follow-ups and reranking at once. There is no second place to forget.

Model:
  document ACL   a list of group names; "*" means any caller (public). An EMPTY list means nobody
                 (fail closed).
  caller groups  the groups of the signed-in user; () for an anonymous caller;
                 None = unrestricted, used only by offline evaluation and admin tooling.
  allowed        document ACL contains "*", or shares at least one group with the caller.

Rules live in data/<dataset>/acl.json next to corpus/:
  {"default": ["all-staff"],
   "rules": [{"match": "17_*", "groups": ["exec"]},        # first matching glob wins
             {"match": "01_*", "groups": ["*"]}]}
No acl.json means a single-user corpus: everything is public.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path


def can_see(acl, groups) -> bool:
    if groups is None:
        return True
    acl = set(acl or ())
    return "*" in acl or bool(acl & set(groups))


def load_acl_rules(corpus_dir: str | Path) -> dict | None:
    p = Path(corpus_dir).parent / "acl.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def acl_for(source: str, rules: dict | None) -> list[str]:
    if rules is None:
        return ["*"]
    for r in rules.get("rules", []):
        if fnmatch.fnmatch(source, r["match"]):
            return list(r["groups"])
    return list(rules.get("default", []))  # unlisted documents get the default; no default = nobody


def acl_fingerprint(corpus_dir: str | Path) -> str:
    """Hash of the rules. Stored in the index; the service refuses to start if it differs from the
    current file, so a stale index can never serve with outdated permissions."""
    rules = load_acl_rules(corpus_dir)
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:16]
