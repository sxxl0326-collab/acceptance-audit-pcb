# Acceptance Audits for Cooperative Design

This repository contains the code and saved evidence for the printed circuit board (PCB) execution study. It includes the versioned SQLite controller, contract compiler, source checks, replay code, and the frozen FactorySimPy catalogue. The PCB requests are authored research scenarios. The simulation does not represent measurements from a production line.

## Reproduce the matched PCB results

Use Python 3.12 or later. Install the replay dependencies, then run the following from this directory.

```sh
python -m pip install numpy scipy simpy==4.1.1
python reproduce_pcb.py
```

The command uses saved contract outputs, checks each resulting SQLite transaction, and prints valid, invalid, and no-new-commit totals. It also reports deferral events. It requires no model API, model weights, or credentials. SQLite read-backs are retained in the temporary directory printed by the command. Advisory and Empty share the same frozen simulator catalogue as EWDV. The reference-contract row uses Advisory with fresh centralized replanning.

The expected fresh centralized results are 56/0/40 for Literal with all three gates, 41/22/33 for Source-parameter under Advisory and 33/0/63 under Empty and EWDV, and 50/4/42 for Numeric feedback under Advisory and Empty and 49/0/47 under EWDV. Reference contracts under Advisory also produce 56/0/40. The Source-parameter Contract controller produces 22/12/62 before gating and 14/0/82 after gating.

## Files

- `reproduce_pcb.py` reproduces the main matched PCB comparison from compiled contracts and a frozen simulation catalogue.
- The `cscwd2027_acceptance_audit_union_20261121` directory contains the original gate and replay scripts, scoped fault probes, outcome summaries and recovery groups.
- The `cscwd2027_acceptance_audit_direction_20261120` directory contains the Direction procedure, authored fixture definitions and saved replay records.
- The `cscwd2027_audit_semantics_policy_revision_20261029` directory contains the final diagnostics and constraint rows used as replay inputs.
- The `cscwd2027_acceptance_audit_revision_20261029` directory contains source requests, saved PCB prompts and responses, compilation and runtime code, simulator manifests and the two Fig. 4 traces. Some archived scripts also describe broader experiments and require data beyond this PCB release.

The FactorySimPy source is vendored at commit `1cc6ad89e01a277662e68d16ca91d4cc5ef956c2` under its included MIT license.
