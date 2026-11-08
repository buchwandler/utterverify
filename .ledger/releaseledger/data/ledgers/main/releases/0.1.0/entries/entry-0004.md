---
schema_version: 2
object_type: release_entry
versioning:
  schema_version: 1
  revision: 2
entry_id: entry-0004
release_version: 0.1.0
kind: added
summary:
  Added direct configuration and segment-level provenance to human and JSON
  verification results
status: accepted
audience: null
scopes: []
source_refs: []
paths:
  - utterverify/cli.py
  - utterverify/types.py
issues: []
prs: []
sources: []
contributors: []
breaking: false
internal: false
order: 4
---

Direct and bundle workflows report reference text, plan and manifest identities, WAV hashes, reports, and per-segment statuses in human and JSON output; fail-fast results identify skipped occurrences as not evaluated.
