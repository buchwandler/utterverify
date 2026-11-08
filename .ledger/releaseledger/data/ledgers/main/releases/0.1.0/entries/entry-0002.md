---
schema_version: 2
object_type: release_entry
versioning:
  schema_version: 1
  revision: 2
entry_id: entry-0002
release_version: 0.1.0
kind: fixed
summary:
  Fixed bundle verification to reject mismatched plan identity and rendered
  artifacts before ASR
status: accepted
audience: null
scopes: []
source_refs: []
paths:
  - utterverify/bundle.py
  - utterverify/core.py
issues: []
prs: []
sources: []
contributors: []
breaking: false
internal: false
order: 2
---

Bundle verification validates set, chapter, and plan identities plus complete manifests, canonical WAV paths, and recorded WAV hashes before ASR; any preflight mismatch stops evaluation.
