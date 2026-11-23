"""Freeze and evaluate a direction-only check against saved and injected contracts."""
from collections import Counter
from hashlib import sha256
from itertools import product
from pathlib import Path
import copy
import csv
import json
import re
import sqlite3
import sys

import numpy as np

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent / "cscwd2027_audit_semantics_policy_revision_20261029"
OLD_FIXTURES = ROOT.parent / "cscwd2027_acceptance_audit_review_20261120/gate_challenge/frozen.json"
sys.path.insert(0, str(BASE))
from replay_policies import DEFAULT, SCI, RowCache, Session, SimulationCache, actual_ok, decision, gated_commit
from benchmark import digest, feasible

OUT = ROOT / "replay"
READ = BASE / "policy_replay"
CONTROLLERS = ("global_cas", "complete_readset", "semantic_guard", "compiled_contract", "central_fresh")
OLD_POLICIES = ("advisory", "empty_defer", "warning_defer")
NEW_POLICIES = ("direction_defer", "warning_direction_defer")
RACES = ("clean", "boundary_revision", "packing_slow", "duplicate")
METRIC = re.compile(r"\b(?:throughput|(?:mean\s+)?cycle[ -]time)\b", re.I)
SPLIT = re.compile(r"[.;\n](?!\d)|\band\s+(?=(?:the\s+)?(?:mean\s+)?(?:cycle[ -]time|throughput)\b)", re.I)
CUES = {
    "ge": re.compile(r"\b(?:at least|no less than|not less than|minimum|floor|no fewer than)\b", re.I),
    "le": re.compile(r"\b(?:at most|no more than|not more than|cannot exceed|maximum|ceiling|cap)\b", re.I),
}
OWNERS = ("production", "quality")


def read(p):
    return json.loads(p.read_text(encoding="utf-8"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def role_cues(text):
    found = {}
    for clause in SPLIT.split(text):
        names = {0 if m.group().lower() == "throughput" else 1 for m in METRIC.finditer(clause)}
        hits = [cmp for cmp, pattern in CUES.items() if pattern.search(clause)]
        for i in names:
            found.setdefault(i, set()).update(hits if len(names) == 1 and len(hits) == 1 else ["ambiguous"])
    return {i: next(iter(c)) for i, c in found.items() if len(c) == 1 and "ambiguous" not in c}


def direction_warnings(text, rows):
    cues = role_cues(text)
    warnings = []
    for j, row in enumerate(rows):
        support = [i for i, a in enumerate(row["a"]) if abs(a) > 1e-9]
        if len(support) != 1 or row["cmp"] not in ("ge", "le"):
            continue
        i = support[0]
        if i not in cues:
            continue
        direction = row["cmp"] if row["a"][i] > 0 else {"ge": "le", "le": "ge"}[row["cmp"]]
        if direction != cues[i]:
            warnings.append(dict(type="direction_mismatch", row=j, metric_index=i, role=OWNERS[i],
                                 source_direction=cues[i], generated_direction=direction))
    return warnings


def row(i, direction, bound, negative=False):
    a = [0, 0]
    a[i] = -1 if negative else 1
    return dict(a=a, cmp=direction, b=bound, cid=f"s{i}")


def fixtures():
    types = ("inverted", "equivalent", "wrong_threshold", "misbound", "warned_partial", "silent_empty")
    production = ("Throughput must be at least {v} boards per minute.",
                  "Require a minimum of {v} boards per minute for throughput.",
                  "The throughput must be no less than {v} boards per minute.",
                  "Production sets a throughput floor of {v} boards per minute.")
    quality = ("Mean cycle time must be at most {v} seconds.",
               "For cycle time, the maximum is {v} seconds.",
               "The mean cycle time cannot exceed {v} seconds.",
               "Quality sets a cycle time ceiling of {v} seconds.")
    cases = []
    for (b, kind), i in product(enumerate(types), range(4)):
        floor = (7.25, 9.25, 11.25, 13.25)[i] + b / 10
        cap = (85, 105, 125, 165)[i] + b
        source = production[i].format(v=f"{floor:.2f}") + " " + quality[i].format(v=cap)
        gold = [row(0, "ge", floor), row(1, "le", cap)]
        original = []
        if kind == "inverted":
            bad = i % 2
            rows = gold.copy()
            rows[bad] = row(bad, gold[bad]["cmp"], gold[bad]["b"], negative=True)
        elif kind == "equivalent":
            rows = [row(0, "le", -floor, negative=True), row(1, "ge", -cap, negative=True)]
        elif kind == "wrong_threshold":
            rows = [gold[0], row(1, "le", cap + 35)]
        elif kind == "misbound":
            rows = [row(1, "ge", floor), gold[1]]
        elif kind == "warned_partial":
            rows = [gold[0]]
            original = [dict(type="uncovered_boundary", cid="s1", role="quality")]
        else:
            rows = []
        cases.append(dict(id=f"probe-{b}-{i}", block=kind, source=source, gold=gold, rows=rows, warnings=original))
    return cases


def old_fixtures():
    return [dict(id=c["case_id"], block=c["case_id"].rsplit("-", 1)[0],
                 source=" ".join(r["text"] for r in c["request"]),
                 gold=c["source_reference"], rows=c["candidate"], warnings=c["warnings"])
            for c in read(OLD_FIXTURES)["cases"]]


def freeze_record():
    paths = [Path(__file__), ROOT / "PROTOCOL.md", OLD_FIXTURES,
             READ / "contract_diagnostics.json", READ / "records.jsonl",
             BASE / "replay_policies.py", SCI / "engineering_cases.json",
             SCI / "engineering_execute.py", SCI / "backend/simulator.py", SCI / "backend/contracts.py",
             SCI / "backend/runtime.py", SCI / "backend/simulation_table.json", SCI / "backend/simulation_manifest.json"]
    return dict(sha256={p.relative_to(ROOT.parent).as_posix():sha256(p.read_bytes()).hexdigest() for p in paths},
                new_fixtures=fixtures(), old_fixtures=old_fixtures())


def policy_reason(policy, rows, warnings, direction):
    if policy == "direction_defer":
        return "DEFER_DIRECTION_MISMATCH" if direction else None
    if policy == "warning_direction_defer":
        return "DEFER_DIRECTION_MISMATCH" if direction else decision("warning_defer", rows, warnings)
    return decision(policy, rows, warnings)


def replay(base, cache, truth, race, controller, op, path, reason):
    session = Session(cache, cache.contracts(), cache.lookup(DEFAULT, "nominal", cache.identity)["metrics"], db=path)
    try:
        proposal = session.prepare(op)
        session.perturb(race, op)
        before = session.state()
        if proposal is None and controller == "central_fresh":
            proposal = dict(op_id=op, base_version=before["version"],
                            read_versions=copy.deepcopy(before["versions"]), patch={}, certificate_identity=cache.identity)
        result = gated_commit(session, proposal, controller, reason) if proposal is not None else None
        state = session.state()
    finally:
        session.close()
    status = "NO_PROPOSAL" if result is None else result["status"]
    committed = status == "COMMITTED"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        stored = json.loads(db.execute("SELECT state FROM project WHERE id=1").fetchone()[0])
        effects = db.execute("SELECT status FROM operations ORDER BY attempt").fetchall()
    assert stored == state and effects.count(("COMMITTED",)) == int(committed)
    if status.startswith("DEFER"):
        assert state == before and not effects
    good = committed and actual_ok(base, state["config"], state["context"], truth, race)
    return dict(status=status, committed=int(committed), valid=int(good), invalid=int(committed and not good),
                config=state["config"], context=state["context"], database=path.name)


def violations(base, record, gold):
    config, context = record["config"], record["context"]
    m = base.lookup(config, context, base.identity)["metrics"]
    x = [m["throughput"], m["cycle_time"]]
    roles = set()
    for row in gold:
        if not feasible([row], x):
            roles.update(OWNERS[i] for i, a in enumerate(row["a"]) if abs(a) > 1e-9)
    if record["race"] == "boundary_revision" and m["inspection_time"] < 3.5 - 1e-8:
        roles.add("quality")
    return sorted(roles)


def ci_by_cluster(rs, key, field, denom):
    ids = sorted({key(r) for r in rs})
    a = np.array([sum(r[field] for r in rs if key(r) == i) for i in ids])
    n = np.array([sum(1 for r in rs if key(r) == i) for i in ids])
    rng = np.random.default_rng(271120)
    z = rng.integers(len(ids), size=(5000, len(ids)))
    rate = denom * a[z].sum(axis=1) / n[z].sum(axis=1)
    return [round(float(q), 3) for q in np.quantile(rate, [0.025, 0.975])]


def analyses(base, records, source_by_id, repairs):
    key = lambda r: (r["group"], r["id"], r["method"], r["model"], r["controller"], r["race"])
    advisory = {key(r): r for r in records if r["policy"] == "advisory"}
    for r in records:
        c = source_by_id[r["id"]]
        b = advisory[key(r)]
        deferred = r["status"].startswith("DEFER")
        r["deferred"] = int(deferred)
        r["prevented"] = int(deferred and b["invalid"])
        r["lost_valid"] = int(deferred and b["valid"])
        r["invalid_roles"] = violations(base, r, c["gold"]) if r["invalid"] else []
        r["oracle_recovered"] = int(deferred and repairs[(r["id"], r["race"])]["valid"])
        r["review_owner"] = None
        if deferred:
            direction = r.get("direction_warnings", [])
            owner = sorted({d["role"] for d in direction})
            # Only Direction policies can claim direction-based localization.
            if r["policy"] in NEW_POLICIES and len(owner) == 1:
                r["review_owner"] = owner[0]
            else:
                r["review_owner"] = "joint"

    slices = sorted({(r["group"], r["method"], r["controller"], r["policy"]) for r in records})
    result = []
    for group, method, controller, policy in slices:
        rs = [r for r in records if (r["group"], r["method"], r["controller"], r["policy"]) == (group, method, controller, policy)]
        nondup = [r for r in rs if r["race"] != "duplicate"]
        fields = ("valid", "invalid", "deferred", "prevented", "lost_valid", "oracle_recovered")
        counts = {field:sum(r[field] for r in rs) for field in fields}
        result.append(dict(group=group, method=method, controller=controller, policy=policy, n=len(rs),
                           nonduplicate_n=len(nondup), requests=len({r["id"] for r in rs}),
                           invalid_request_ids=sorted({r["id"] for r in rs if r["invalid"]}),
                           invalid_roles=dict(Counter(ro for r in rs for ro in r["invalid_roles"])),
                           reviews_by_role=dict(Counter(r["review_owner"] for r in rs if r["review_owner"])),
                           review_groups=len({(r["id"], r["method"], r["model"]) for r in rs if r["deferred"]}),
                           reviews_per_prevented_invalid=(counts["deferred"]/counts["prevented"] if counts["prevented"] else None),
                           prevented_pp_ci=ci_by_cluster(nondup, lambda r:r["id"],"prevented",100) if group=="saved" else None,
                           lost_valid_pp_ci=ci_by_cluster(nondup, lambda r:r["id"],"lost_valid",100) if group=="saved" else None,
                           wording_sensitivity_pp_ci=ci_by_cluster(nondup,lambda r:int(r["id"].split(":")[1])%6,"prevented",100) if group=="saved" else None,
                           **counts))
    return result


def run():
    frozen = read(OUT / "frozen.json")
    assert frozen == freeze_record()
    dbdir = OUT / "databases"
    dbdir.mkdir()
    base = SimulationCache()
    saved = [json.loads(s) for s in (READ / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    reference = {c["id"]: dict(c,source=c["text"]) for c in read(SCI / "engineering_cases.json")}
    for c in frozen["new_fixtures"] + frozen["old_fixtures"]:
        assert c["id"] not in reference
        reference[c["id"]] = c
    old_control = {(r["id"],r["model"],r["method"],r["race"],r["controller"]):r for r in saved if r["policy"]=="advisory"}
    records = [dict(r,group="saved") for r in saved]
    annotated = {}
    for c in read(READ / "contract_diagnostics.json"):
        case_id, model, method = c["id"], c["model"], c["method"]
        truth = reference[case_id]
        rows, warnings = c["contract"], c["final_diagnostics"]
        direction = direction_warnings(truth["source"], rows)
        annotated[(case_id,model,method)] = direction
        cache = RowCache(base, rows)
        for race, controller, policy in product(RACES, CONTROLLERS, NEW_POLICIES):
            op = "/".join([case_id,model,method,race,controller])
            path = dbdir / (digest([policy,op])[:24] + ".sqlite")
            assert not path.exists()
            d = replay(base,cache,truth["gold"],race,controller,op,path,policy_reason(policy,rows,warnings,direction))
            if not d["status"].startswith("DEFER"):
                adv=old_control[(case_id,model,method,race,controller)]
                assert all(d[k]==adv[k] for k in ("status","committed","valid","invalid","config","context"))
            records.append(dict(d,group="saved",id=case_id,model=model,method=method,race=race,controller=controller,policy=policy))
    for r in records:
        r["direction_warnings"] = annotated[(r["id"],r["model"],r["method"])]
    print("Saved scenarios checked:", len(records), flush=True)
    for group in ("old_fixtures", "new_fixtures"):
        for c in frozen[group]:
            rows, warnings = c["rows"], c["warnings"]
            direction = direction_warnings(c["source"],rows)
            cache = RowCache(base, rows)
            for race,controller,policy in product(RACES[:2],CONTROLLERS[-2:],OLD_POLICIES+NEW_POLICIES):
                op="/".join([c["id"],race,controller])
                path=dbdir/(digest([group,policy,op])[:24]+".sqlite")
                assert not path.exists()
                d=replay(base,cache,c["gold"],race,controller,op,path,policy_reason(policy,rows,warnings,direction))
                records.append(dict(d,group=group,id=c["id"],model="injected",method="all",block=c["block"],
                                    race=race,controller=controller,policy=policy,direction_warnings=direction))
    repairs = {}
    for case_id,race in sorted({(r["id"],r["race"]) for r in records if r["status"].startswith("DEFER")}):
        rows = reference[case_id]["gold"]
        cache = RowCache(base,rows)
        op=f"ideal-review/{case_id}/{race}"
        path=dbdir/(digest(op)[:24]+".sqlite")
        d=replay(base,cache,rows,race,"central_fresh",op,path,None)
        assert not d["invalid"]
        repairs[case_id,race]=dict(d,id=case_id,race=race)
    summary=analyses(base,records,reference,repairs)
    with (OUT/"records.jsonl").open("w",encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r,ensure_ascii=False)+"\n")
    save("summary.json",summary)
    save("ideal_repairs.json",list(repairs.values()))
    assert len(records)==15040
    assert len(list(dbdir.glob("*.sqlite")))==5760+640+len(repairs)
    print(f"{len(records)} matched scenarios, {len(repairs)} ideal-repair executions.",flush=True)


if __name__=="__main__":
    if sys.argv[1:]==["freeze"]:
        OUT.mkdir(exist_ok=True)
        with (OUT/"frozen.json").open("x",encoding="utf-8") as f:
            json.dump(freeze_record(),f,ensure_ascii=False,indent=2)
    elif sys.argv[1:]==["run"]:
        run()
    else:
        raise SystemExit("Usage: direction_replay.py freeze | run")
