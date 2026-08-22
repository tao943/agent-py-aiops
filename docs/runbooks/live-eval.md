# Order Pool Automatic Closure

This acceptance path validates the real Single-Agent lifecycle:

```text
Order API fault → Prometheus → Alertmanager webhook → PostgreSQL job
→ Single-Agent diagnosis → deterministic authorization → Compose restart
→ independent verification → resolved and verified Incident
```

## Prerequisites

- PostgreSQL, Redis, Nginx, Alertmanager, Milvus, and the backend worker are healthy.
- The backend was started with the same project configuration passed to the script.
- The selected alert source belongs to the supplied owner and knowledge base.
- Required model, CLS (when selected), and webhook credentials are already configured through the
  project's ignored local JSON configuration files.

The script does not read or print credential values and never invokes a manual alert publisher.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_order_pool_auto_closure.ps1 `
  -RunId "auto-closure-20260822-001" `
  -OwnerUserId "<owner>" `
  -KnowledgeBaseId "kb_<owner>" `
  -ConfigPath "<absolute-project-config-path>" `
  -EvidenceSource local
```

Use `-Resume` only with the exact same run ID after an interrupted running evaluation. A terminal run is returned without replaying recovery.

## Acceptance evidence

Record only safe values:

- Git SHA and service image IDs
- run, Incident, diagnostic Task, Job, Report, and recovery-intent IDs
- MTTD, diagnosis, recovery, verification, resolved, and MTTR durations
- deterministic score, validity, and six verification check results
- whether the recovery execution was reused

Never record configuration contents, tokens, raw provider responses, Docker arguments, or exception text.
