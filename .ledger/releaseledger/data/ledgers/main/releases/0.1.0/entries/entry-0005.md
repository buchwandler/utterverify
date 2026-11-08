---
schema_version: 2
object_type: release_entry
versioning:
  schema_version: 1
  revision: 2
entry_id: entry-0005
release_version: 0.1.0
kind: changed
summary:
  Changed doctor checks to validate RenderPlan compatibility and whisper.cpp
  readiness
status: accepted
audience: null
scopes: []
source_refs: []
paths:
  - utterverify/doctor.py
  - tests/test_doctor.py
issues: []
prs: []
sources: []
contributors: []
breaking: false
internal: false
order: 5
---

Doctor checks the required RenderPlan and SpeechScope contracts and reports whisper.cpp executable and model readiness independently without inference or model acquisition.
