# Experiment companions

The logs, dedicated configurations and original helper scripts document this local experiment. Configuration files refer to password environment variables; credential values and environment files are not archived. Some original helpers retain the original local paths and are included for provenance, not as general command-line interfaces.

The native reproducible commands are in ../registered/README.md. Standard cases can be replayed from their original case directories with Src/main.py and a dedicated configuration. Compact registered records cannot be passed to main.py --replay.

Recheck report queries, Pattern folders, versions, all 40 pairs, controls and thresholds without database access:

```bash
python results/detection-20261009/experiment/summarize_issues.py
python Src/summarize.py --results results/detection-20261009/registered --bugs bugs.csv --output results/detection-20261009/registered/summary
```

The validator only changes repository path discovery in this archived copy. It recalculates paired medians and a 95% bootstrap interval with seed 0; source intervals are retained separately.
