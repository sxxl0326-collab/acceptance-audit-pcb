"""Freeze and replay final contracts with faults outside the unary gate checks."""
import argparse
from collections import Counter
from hashlib import sha256
from itertools import product
from pathlib import Path

import audit

D = audit.D
BASE = audit.ROOT.parent
OUT = audit.OUT / "scope_probe"
SCRIPTS = ("clean", "boundary_revision")
POLICIES = ("advisory", "ewdv")
FLOOR = "Throughput must be at least 8 boards per minute."
CEILING = "Mean cycle time must be at most 100 seconds."


def coupled(a, b):
    return {"a": [a, 1], "cmp": "le", "b": b, "cid": "s1"}


def inputs():
    throughput = D.row(0, "ge", 8)
    cycle = D.row(1, "le", 100)
    mixed = "Mean cycle time plus twice the throughput reading must be at most 130 on the combined score."
    return [
        dict(id="mixed_wrong_sign", fault="Multivariable sign",
             source=f"{FLOOR} {mixed}", gold=[throughput, coupled(2, 130)],
             candidate=[throughput, coupled(-2, 130)]),
        dict(id="mixed_coefficient_scale", fault="Multivariable coefficient scale",
             source=f"{FLOOR} {mixed}", gold=[throughput, coupled(2, 130)],
             candidate=[throughput, coupled(0.2, 130)]),
        dict(id="milliseconds_as_seconds", fault="Unit conversion",
             source=f"{FLOOR} Mean cycle time must be at most 100000 milliseconds.",
             gold=[throughput, cycle], candidate=[throughput, D.row(1, "le", 100000)]),
        dict(id="minutes_as_seconds", fault="Unit conversion",
             source=f"{FLOOR} Mean cycle time must be at most 1.5 minutes.",
             gold=[throughput, D.row(1, "le", 90)], candidate=[throughput, D.row(1, "le", 1.5)]),
        dict(id="missing_cycle_cap", fault="Silent partial omission",
             source=f"{FLOOR} {CEILING}", gold=[throughput, cycle], candidate=[throughput]),
        dict(id="missing_throughput_floor", fault="Silent partial omission",
             source=f"Throughput must be at least 15 boards per minute. {CEILING}",
             gold=[D.row(0, "ge", 15), cycle], candidate=[cycle]),
    ]


def receipt():
    files = [Path(__file__), Path(audit.__file__), Path(D.__file__),
             D.BASE / "replay_policies.py", D.SCI / "backend/runtime.py",
             D.SCI / "backend/simulation_table.json", D.SCI / "backend/simulation_manifest.json"]
    return {
        "cases": inputs(),
        "scripts": list(SCRIPTS),
        "controllers": list(audit.CONTROLLERS),
        "policies": list(POLICIES),
        "warning_context": "Nonempty final compiled rows with no inherited Warning diagnostic.",
        "sha256": {path.resolve().relative_to(BASE).as_posix(): sha256(path.read_bytes()).hexdigest()
                   for path in files},
    }


def write(filename, data):
    import json
    (OUT / filename).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run(frozen):
    dbdir = OUT / "databases"
    dbdir.mkdir()
    base = D.SimulationCache()
    records = []
    for case in frozen["cases"]:
        rows = case["candidate"]
        truth = case["gold"]
        eq = audit.E.check(rows, truth)
        assert not eq["region_equal"], case["id"]
        direction = D.direction_warnings(case["source"], rows)
        value = audit.value_warnings(case["source"], rows)
        cache = D.RowCache(base, rows)
        for controller, script in product(frozen["controllers"], frozen["scripts"]):
            pair = []
            for policy in frozen["policies"]:
                # Empty and Warning cannot fire on a nonempty, warning-free final input.
                reason = None
                if policy == "ewdv":
                    if not rows:
                        reason = "DEFER_EMPTY_REQUIRED_CONTRACT"
                    elif direction:
                        reason = "DEFER_DIRECTION_MISMATCH"
                    elif value:
                        reason = "DEFER_VALUE_MISMATCH"
                op = f"scope/{case['id']}/{controller}/{script}"
                path = dbdir / f"{D.digest([op, policy])[:24]}.sqlite"
                result = D.replay(base, cache, truth, script, controller, op, path, reason)
                rec = dict(id=case["id"], fault=case["fault"], controller=controller, script=script,
                           policy=policy, reason=reason, direction=direction, value=value, **result)
                pair.append(rec)
                records.append(rec)
            if not pair[1]["status"].startswith("DEFER"):
                assert all(pair[0][k] == pair[1][k] for k in ("status", "valid", "invalid", "config", "context"))
    write("records.json", records)
    summary = []
    for axis in ("all", *[c["id"] for c in frozen["cases"]]):
        for ctl in frozen["controllers"]:
            for gate in frozen["policies"]:
                selected = [r for r in records if (axis == "all" or r["id"] == axis)
                            and r["controller"] == ctl and r["policy"] == gate]
                totals = Counter(r["status"] for r in selected)
                summary.append(dict(case=axis, controller=ctl, policy=gate, attempts=len(selected),
                                    valid=sum(r["valid"] for r in selected),
                                    invalid=sum(r["invalid"] for r in selected),
                                    deferred=sum(n for k, n in totals.items() if k.startswith("DEFER")),
                                    statuses=dict(totals)))
    write("summary.json", summary)
    for row in summary:
        if row["policy"] == "ewdv":
            print(row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("freeze", "run"))
    args = parser.parse_args()
    snapshot = receipt()
    if args.stage == "freeze":
        OUT.mkdir()
        write("frozen.json", snapshot)
    else:
        assert D.read(OUT / "frozen.json") == snapshot
        run(snapshot)
