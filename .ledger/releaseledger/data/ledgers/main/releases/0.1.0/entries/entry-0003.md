---
schema_version: 2
object_type: release_entry
versioning:
  schema_version: 1
  revision: 2
entry_id: entry-0003
release_version: 0.1.0
kind: changed
summary:
  Changed verification to use prepared RenderPlan text with compound-aware
  exact-content thresholds
status: accepted
audience: null
scopes: []
source_refs: []
paths:
  - utterverify/core.py
  - README.md
issues: []
prs: []
sources: []
contributors: []
breaking: false
internal: false
order: 3
---

SpeechScope receives each prepared SpeechItem.text as its reference. The default requires zero compound-aware lexical and character error while allowing compound-boundary variants; it is not strict whitespace-tokenized word equality.
