"""Replay policy unions and audit saved unary bounds without new inference."""
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import product
from math import isclose
from pathlib import Path
import csv
import json
import re
import sys

ROOT = Path(__file__).resolve().parent
PREV = ROOT.parent / "cscwd2027_acceptance_audit_direction_20261120"
sys.path.insert(0, str(PREV))
import direction_replay as D
import ledger
import offline_regions as E

OUT = ROOT / "evidence"
METHODS = ("parametric_full", "numeric_feedback")
CONTROLLERS = D.CONTROLLERS[-2:]
UNION = ("ewd", "ewdv")
POLICIES = D.OLD_POLICIES + ("direction_defer", "warning_direction_defer") + UNION
INPUTS = [PREV / "replay" / name for name in ("records.jsonl","frozen.json","ideal_repairs.json")]
INPUTS += [PREV / "replication/scored_summary.json", D.READ / "contract_diagnostics.json",
           D.SCI / "engineering_cases.json", D.SCI / "test_cases.json", D.SCI / "all_results.json",
           D.SCI / "ledger.py", D.SCI / "equivalence.py", D.SCI / "offline_regions.py",
           D.SCI / "backend/runtime.py", D.SCI / "backend/simulation_table.json",
           PREV / "direction_replay.py", Path(__file__), ROOT / "PROTOCOL.md"]
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
UNITS = (re.compile(r"\bboards?\s+per\s+minute\b",re.I), re.compile(r"\bseconds?\b",re.I))


def save(name, value):
    (OUT / name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def csv_out(name, records):
    with (OUT / name).open("w",encoding="utf-8-sig",newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def unary(row):
    nonzero = [i for i,a in enumerate(row["a"]) if abs(a)>1e-9]
    if len(nonzero)!=1 or row["cmp"] not in {"ge","le"}:
        return None
    i = nonzero[0]
    a = row["a"][i]
    return i, row["cmp"] if a>0 else {"le":"ge","ge":"le"}[row["cmp"]], row["b"]/a


def pcb_values(text):
    candidates = defaultdict(set)
    directions = D.role_cues(text)
    for clause in D.SPLIT.split(text):
        cues = D.role_cues(clause)
        numbers = NUMBER.findall(clause)
        if len(cues)==1 and len(numbers)==1:
            i, direction = next(iter(cues.items()))
            if directions.get(i)==direction and UNITS[i].search(clause):
                candidates[i].add(float(numbers[0]))
    return {i:next(iter(xs)) for i,xs in candidates.items() if len(xs)==1}


def value_warnings(text, rows):
    expected = pcb_values(text)
    directions = D.role_cues(text)
    flags = []
    for j,row in enumerate(rows):
        eligible = unary(row)
        if eligible is None:
            continue
        i, cmp, b = eligible
        if i in expected and cmp==directions[i] and not isclose(b,expected[i],rel_tol=1e-8,abs_tol=1e-7):
            flags.append(dict(row=j,variable=i,source=expected[i],candidate=b))
    return flags


def key(r):
    return r["group"],r["id"],r["method"],r["model"],r["controller"],r["race"]


def generated_replays(base, previous, contexts):
    advisory = {key(r):r for r in previous if r["policy"]=="advisory"}
    flags = {}
    repairs = {(r["id"],r["race"]):r["valid"]
               for r in D.read(PREV / "replay/ideal_repairs.json")}
    new = []
    for k,args in contexts.items():
        source, truth, rows, warns = args
        direction = D.direction_warnings(source,rows)
        value = value_warnings(source,rows)
        flags[k] = dict(group=k[0], id=k[1], method=k[2], model=k[3],
                        rows=rows, original_warnings=warns, direction=direction, value=value,
                        reference_region_equal=E.check(rows,truth)["region_equal"])
        cache = D.RowCache(base, rows)
        scripts = D.RACES if k[0]=="saved" else D.RACES[:2]
        for controller,race,policy in product(CONTROLLERS,scripts,UNION):
            marker = (k[1],race)
            reason = ("DEFER_EMPTY_REQUIRED_CONTRACT" if not rows else
                      "DEFER_UNRESOLVED_DIAGNOSTICS" if warns else
                      "DEFER_DIRECTION_MISMATCH" if direction else
                      "DEFER_VALUE_MISMATCH" if policy=="ewdv" and value else None)
            op = "/".join([*k,controller,race])
            path = OUT / "databases" / f"{D.digest([op,policy])[:24]}.sqlite"
            result = D.replay(base,cache,truth,race,controller,op,path,reason)
            r = dict(result,group=k[0],id=k[1],method=k[2],model=k[3],
                     controller=controller,race=race,policy=policy)
            b = advisory[key(r)]
            deferred = r["status"].startswith("DEFER")
            if deferred and marker not in repairs:
                refcache = D.RowCache(base,truth)
                repairpath = OUT / "databases" / f"{D.digest(['repair',*marker])[:24]}.sqlite"
                repaired = D.replay(base,refcache,truth,race,"central_fresh",
                                    "/".join(marker),repairpath,None)
                assert not repaired["invalid"]
                repairs[marker] = repaired["valid"]
            if not deferred:
                assert all(r[f]==b[f] for f in ("status","valid","invalid","config","context"))
            new.append(dict(r,deferred=int(deferred),prevented=int(deferred and b["invalid"]),
                            lost_valid=int(deferred and b["valid"]),
                            oracle_recovered=repairs[marker] if deferred else 0))
    save("flags.json",list(flags.values()))
    save("union_records.json",new)
    save("recovery_keys.json",[dict(id=i,script=r,valid=v) for (i,r),v in sorted(repairs.items())])
    return new,flags


def policy_summary(records):
    buckets = defaultdict(list)
    for r in records:
        buckets[r["group"],r["method"],r["controller"],r["policy"]].append(r)
    result = []
    fields = ("valid","invalid","deferred","lost_valid","prevented","oracle_recovered")
    for k, rs in sorted(buckets.items()):
        s = dict(zip(("group","method","controller","policy"),k))
        s.update(n=len(rs),requests=len({r["id"] for r in rs}), **{f:sum(r[f] for r in rs) for f in fields})
        baseline = sum(r["valid"] for r in buckets[(*k[:3],"advisory")])
        s["reviews_per_prevented"] = s["deferred"]/s["prevented"] if s["prevented"] else None
        s["after_oracle"] = s["valid"] + s["oracle_recovered"]
        s["net_after_oracle_vs_advisory"] = s["after_oracle"] - baseline
        result.append(s)
    csv_out("policy_summary.csv",result)
    save("policy_summary.json",result)


def risk_replay(base, previous, contexts, flags):
    by_key = {(key(r),r["policy"]):r for r in previous}
    risks = []
    for r in previous:
        if r["policy"]!="advisory" or not r["valid"]:
            continue
        k = key(r)
        addition = by_key[k,"warning_direction_defer"]["deferred"] and not by_key[k,"warning_defer"]["deferred"]
        if not addition or not flags[k[:4]]["direction"]:
            continue
        _, truth, rows, _ = contexts[k[:4]]
        result = dict(group=r["group"],id=r["id"],method=r["method"],model=r["model"],
                      controller=r["controller"],focus=r["race"],
                      reference_defect=not flags[k[:4]]["reference_region_equal"],
                      same_contract_other_scripts={},other_capacity_same_script=None,
                      other_capacity_other_scripts={})
        models = [r["model"]] if r["model"]=="injected" else ["qwen3.5:4b","qwen3.5:9b"]
        for model,race in product(models,D.RACES):
            if model==r["model"] and race==r["race"]:
                continue
            q = (*k[:3],model,k[4],race)
            _, candidate_truth, candidate_rows, _ = contexts[q[:4]]
            cache = D.RowCache(base,candidate_rows)
            op = "/".join(q)
            path = OUT/"databases"/f"{D.digest(['risk',*q])[:24]}.sqlite"
            if not path.exists():
                fresh = D.replay(base,cache,candidate_truth,race,k[4],op,path,None)
            else:
                fresh = reused[q]
            if (q,"advisory") in by_key:
                assert all(fresh[f]==by_key[q,"advisory"][f] for f in ("status","config","valid","invalid"))
            reused[q] = fresh
            if model==r["model"]:
                result["same_contract_other_scripts"][race] = fresh
            elif race==r["race"]:
                result["other_capacity_same_script"] = fresh
            else:
                result["other_capacity_other_scripts"][race] = fresh
        risks.append(result)
    save("risk_focus.json",risks)
    reports = []
    for (group,method,controller), rs in sorted(grouped(risks,lambda r:(r["group"],r["method"],r["controller"])).items()):
        distinct = {(r["id"],r["model"]) for r in rs}
        others = {(r["id"],r["model"],script):z for r in rs for script,z in r["same_contract_other_scripts"].items()}
        other_caps = {(r["id"],r["model"],script):z for r in rs
                      for script,z in ({**r["other_capacity_other_scripts"],r["focus"]:r["other_capacity_same_script"]}).items() if z}
        reports.append(dict(group=group,method=method,controller=controller,focus_valid=len(rs),
             distinct_flagged_contracts=len(distinct),defective_contracts=len({(r["id"],r["model"]) for r in rs if r["reference_defect"]}),
             same_contract_other_outcomes=len(others),same_contract_other_commits=sum(z["committed"] for z in others.values()),
             same_contract_other_invalid=sum(z["invalid"] for z in others.values()),
             contracts_invalid_elsewhere=len({(i,m) for (i,m,_),z in others.items() if z["invalid"]}),
             alternate_capacity_outcomes=len(other_caps),alternate_capacity_commits=sum(z["committed"] for z in other_caps.values()),
             alternate_capacity_invalid=sum(z["invalid"] for z in other_caps.values())))
    save("risk_summary.json",reports)
    csv_out("risk_summary.csv",reports)


def grouped(xs,keyfn):
    out = defaultdict(list)
    for x in xs:
        out[keyfn(x)].append(x)
    return out


def nl_audit():
    cases = {c["id"]:c for c in D.read(D.SCI/"test_cases.json")}
    outputs = [r for r in D.read(D.SCI/"all_results.json") if r["dataset"]=="nl4opt_new"]
    assert len(outputs)==64*2*27
    cues = {}
    reference = {}
    for i,c in cases.items():
        candidates = defaultdict(set)
        for q in ledger.inventory(c):
            if q["var_hint"] is not None and q["direction"] and not q["percent"] and not q["multiple"] and q["role"]!="schema_count":
                candidates[q["var_hint"]].add(q["direction"])
        cues[i] = {v:next(iter(xs)) for v,xs in candidates.items() if len(xs)==1}
        gold = defaultdict(set)
        for r in c["gold"]:
            u = unary(r)
            if u:
                gold[u[0]].add(u[1])
        reference[i] = {v:next(iter(ds)) for v,ds in gold.items() if len(ds)==1}
    scores = []
    arm_keys = Counter()
    for r in outputs:
        i = r["id"]
        arm_keys[r["model"],r["method"]] += 1
        for j,row in enumerate(r["contract"]):
            u = unary(row)
            if u is None:
                continue
            v,d,b = u
            cue = cues[i].get(v)
            ref = reference[i].get(v)
            scores.append(dict(id=i,model=r["model"],method=r["method"],row=j,variable=v,
                               negative=row["a"][v]<0,cmp=d,bound=b,cue=cue,reference_direction=ref,
                               flag=cue is not None and cue!=d,
                               reference_opposition=None if ref is None else ref!=d))
    assert len(arm_keys)==54 and set(arm_keys.values())=={64}
    csv_out("nl4opt_rows.csv",scores)
    report=[]
    for (model,method), n in sorted(arm_keys.items()):
        rows=[r for r in scores if (r["model"],r["method"])==(model,method)]
        report.append(nl_summary(rows,model=model,method=method,programs=n))
    csv_out("nl4opt_by_arm.csv",report)
    distinct = {(r["id"],r["variable"],r["cmp"],round(r["bound"],8)):r for r in scores}
    aggregate = dict(all_rows=nl_summary(scores),distinct_rows=nl_summary(list(distinct.values())),
                     n_sources=len(cases),n_source_cues=sum(bool(x) for x in cues.values()),programs=len(outputs),
                     arms=len(arm_keys),cue_only=all(set(c).issubset({0,1}) for c in cues.values()))
    save("nl4opt_summary.json",aggregate)


def nl_summary(rows,**data):
    return dict(**data,unary=len(rows),source_eligible=sum(bool(r["cue"]) for r in rows),
        reference_eligible=sum(r["reference_opposition"] is not None for r in rows),
        flags=sum(r["flag"] for r in rows),tp=sum(r["flag"] and r["reference_opposition"] is True for r in rows),
        fp=sum(r["flag"] and r["reference_opposition"] is False for r in rows),
        unresolved_flags=sum(r["flag"] and r["reference_opposition"] is None for r in rows),
        reference_oppositions=sum(r["reference_opposition"] is True for r in rows),
        negative_oppositions=sum(r["negative"] and r["reference_opposition"] is True for r in rows))


if __name__=="__main__":
    signature={p.relative_to(ROOT.parent).as_posix():sha256(p.read_bytes()).hexdigest() for p in INPUTS}
    OUT.mkdir(exist_ok=True)
    if sys.argv[1:]==["freeze"]:
        assert not (OUT/"frozen.json").exists()
        save("frozen.json",signature)
        sys.exit()
    assert sys.argv[1:]==["run"] and signature==D.read(OUT/"frozen.json")
    (OUT/"databases").mkdir()
    previous = [json.loads(s) for s in (PREV/"replay/records.jsonl").read_text(encoding="utf-8").splitlines()]
    previous = [r for r in previous if r["controller"] in CONTROLLERS and
                (r["group"]!="saved" or r["method"] in METHODS)]
    cases = {c["id"]:c for c in D.read(D.SCI/"engineering_cases.json")}
    contexts = {("saved",r["id"],r["method"],r["model"]):
        (cases[r["id"]]["text"],cases[r["id"]]["gold"],r["contract"],r["final_diagnostics"])
        for r in D.read(D.READ/"contract_diagnostics.json") if r["method"] in METHODS}
    for group in ("old_fixtures","new_fixtures"):
        for c in D.read(PREV/"replay/frozen.json")[group]:
            contexts[group,c["id"],"all","injected"] = c["source"],c["gold"],c["rows"],c["warnings"]
    reused = {}
    base = D.SimulationCache()
    fresh,flags = generated_replays(base,previous,contexts)
    assert len(fresh)==1024
    policy_summary(previous+fresh)
    risk_replay(base,previous,contexts,flags)
    nl_audit()
    print("Verified 1,024 union outcomes and audited 3,456 saved NL4Opt programs.")
