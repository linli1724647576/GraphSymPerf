# Bug reports

The [report index](../docs/bug-reports.md) lists the issue links, recorded statuses and available local reports. The [registry](../bugs.csv) contains the status evidence and current pattern assignments.

This directory contains reports submitted to public issue trackers. Reports are grouped by database and named after the public issue number.

Each report gives the tested version, complete graph setup, query pair, results, ordinary runtimes and execution-plan excerpts. Follow the setup in that report to reproduce the case. Source paths and earlier review notes are retained in its closing HTML comment.

For a new performance report, use four sections:

1. **Describe the situation**: give the two complete queries and explain the observed difference.
2. **Which DBMS versions are affected?**: identify the tested version and relevant settings.
3. **How to reproduce**: provide the graph setup, results, timing method and plan excerpts that show the additional work.
4. **Expected performance**: state which work the database could skip, using the faster query as the comparison.

Keep each result, runtime and plan tied to its measured query and graph. Put submission status and later tracker updates in `bugs.csv`.
