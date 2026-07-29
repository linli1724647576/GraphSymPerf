# Validation summary

- Rule family: `BOOL`
- Rule: `case_true_selection`
- Result status: complete results equal
- Current image digest: `sha256:d96360569a515d341800783009f653f413a5ee9b7fa0de74954f68e4cc8e7787`
- Current build: `5de4d02b67a6fbbc07cd243866600cad8172129e`
- Scale: 300,000 vertices
- Session 1: 20 alternating query pairs plus A/A and B/B controls
- Session 2: 20 alternating query pairs plus A/A and B/B controls
- Plan change: `NodeIndexSeek` to `NodeByLabelScan`
- Runtime gate: passed in both sessions

Evidence:

- `artifacts/arcadedb_bool_hunt_20260724/case_current_verify_session1_300k.json`
- `artifacts/arcadedb_bool_hunt_20260724/case_current_verify_session2_300k.json`

The public issue is self-contained and does not require uploading the screening
or validation script.
