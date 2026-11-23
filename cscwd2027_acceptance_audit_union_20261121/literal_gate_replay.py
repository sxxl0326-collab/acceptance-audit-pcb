"""Compare three saved PCB contract sources under the same planner and gates."""
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
import json
import sys

import audit

D = audit.D
ROOT = audit.ROOT
OUT = audit.OUT / "literal_gate"
REPLAY = audit.PREV / "replay/records.jsonl"
UNIONS = audit.OUT / "union_records.json"
DIAGNOSTICS = D.READ / "contract_diagnostics.json"
CASES = D.SCI / "engineering_cases.json"
FORMULAS = D.SCI / "formulas_engineering/raw.jsonl"
NUMERIC = D.SCI / "engineering_language/raw.jsonl"
METHODS = ("literal_full", "parametric_full", "numeric_feedback")
GATES = ("advisory", "empty_defer", "ewdv")


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replay_key(row):
    return row["id"], row["model"], row["race"]


def gate_reason(candidate, source):
    rows = candidate["contract"]
    if not rows:
        return "DEFER_EMPTY_REQUIRED_CONTRACT"
    if candidate["final_diagnostics"]:
        return "DEFER_UNRESOLVED_DIAGNOSTICS"
    if D.direction_warnings(source, rows):
        return "DEFER_DIRECTION_MISMATCH"
    if audit.value_warnings(source, rows):
        return "DEFER_VALUE_MISMATCH"
    return None


def main():
    inputs = (Path(__file__), Path(audit.__file__), Path(D.__file__), REPLAY, UNIONS, DIAGNOSTICS,
              CASES, FORMULAS, NUMERIC, D.SCI / "backend/runtime.py", D.SCI / "backend/simulation_table.json")
    frozen = {p.resolve().relative_to(ROOT.parent).as_posix(): sha256(p.read_bytes()).hexdigest()
              for p in inputs}
    if sys.argv[1:] == ["freeze"]:
        OUT.mkdir()
        write("frozen.json", frozen)
        return
    assert sys.argv[1:] == ["run"] and frozen == D.read(OUT / "frozen.json")
    dbdir = OUT / "databases"
    dbdir.mkdir()
    raw = {r["key"]: r for r in read_lines(FORMULAS) + read_lines(NUMERIC)}
    saved = defaultdict(dict)
    for row in read_lines(REPLAY) + D.read(UNIONS):
        if (row["group"] == "saved" and row["controller"] == "central_fresh"
                and row["method"] in METHODS and row["policy"] in GATES):
            group = saved[row["method"], row["policy"]]
            key = replay_key(row)
            assert key not in group
            group[key] = row

    bases = {(r["id"], r["model"]): r for r in D.read(DIAGNOSTICS) if r["method"] == "literal_full"}
    cases = {r["id"]: r for r in D.read(CASES)}
    assert len(bases) == 24
    stages = {"literal_full": ("literal_initial", "literal_review"),
              "parametric_full": ("parametric_initial", "parametric_review"),
              "numeric_feedback": ("numeric_initial", "numeric_feedback")}
    for i, m in bases:
        for first, second in stages.values():
            assert all(f"{m}/{i}/{stage}" in raw for stage in (first, second)), (m,i,first,second)
    base = D.SimulationCache()
    added = []
    for (i, m), candidate in sorted(bases.items()):
        case = cases[i]
        cache = D.RowCache(base, candidate["contract"])
        reason = gate_reason(candidate, case["text"])
        for script in D.RACES:
            key = i, m, script
            operation = f"literal-gate/{i}/{m}/{script}"
            path = dbdir / f"{D.digest(operation)[:24]}.sqlite"
            row = D.replay(base, cache, case["gold"], script, "central_fresh", operation, path, reason)
            row.update(id=i, model=m, race=script, method="literal_full", policy="ewdv",
                       controller="central_fresh", deferred=int(row["status"].startswith("DEFER")))
            advisory = saved["literal_full", "advisory"][key]
            if not row["deferred"]:
                assert all(advisory[k] == row[k] for k in ("valid", "invalid", "status", "config", "context"))
            saved["literal_full", "ewdv"][key] = row
            added.append(row)
    write("literal_ewdv_records.json", added)

    summary = []
    for method in METHODS:
        s = {"method": method, "policies": {}}
        empty = saved[method, "empty_defer"]
        ewdv = saved[method, "ewdv"]
        new_deferrals = [empty[k] for k, row in ewdv.items() if row["deferred"] and not empty[k]["deferred"]]
        for gate in GATES:
            cell = saved[method, gate]
            assert set(cell) == set(empty) and len(cell) == 96
            rs = list(cell.values())
            s["policies"][gate] = dict(valid=sum(r["valid"] for r in rs), invalid=sum(r["invalid"] for r in rs),
                                        no_commit=sum(not r["committed"] for r in rs),
                                        deferred=sum(r["deferred"] for r in rs),
                                        statuses=dict(sorted(Counter(r["status"] for r in rs).items())))
        s["incremental_over_empty"] = dict(deferred=len(new_deferrals),
                                            invalid_prevented=sum(r["invalid"] for r in new_deferrals),
                                            valid_withheld=sum(r["valid"] for r in new_deferrals))
        summary.append(s)
    write("summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
