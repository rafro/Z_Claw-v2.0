# Artifact Lifecycle — OpenClaw Orchestrator v2
# Applies to: all divisions | Owner: artifact-manager skill

---

## Overview

Assets move through four states. Division orchestrators manage transitions.
J_Claw never touches archives or raw files directly.

```
COLD ──────► MANIFEST ──────► HOT CACHE ──────► PACKET
(archive)    (triage)         (working set)      (to J_Claw)
    ▲                              │
    └──────────────────────────────┘
         rezip at checkpoint only
```

---

## State 1 — Cold Archive

**Format:** `.zip` or `.7z`
**Location:** `divisions/{name}/cold/`
**Purpose:** inactive storage, distribution, versioned snapshots, rollback points

Rules:
- Never open a cold archive without first reading its manifest
- Never rezip a hot cache after every task — only at explicit checkpoint boundaries
- Never store active model files as ZIP — use raw GGUF only
- Naming: `{bundle_id}_{YYYY-MM-DD}.zip`

Examples per division:
- **trading/** — market snapshots, backtest outputs, strategy configs, trade journals
- **opportunity/** — lead lists, company dossiers, scraped research, job bundles
- **dev-automation/** — repo snapshots, build logs, doc bundles, dependency manifests
- **personal/** — health history archives, correlation bundles, schedule drafts

---

## State 2 — Warm Manifest

**Format:** `manifest.json`
**Location:** `divisions/{name}/manifests/`
**Purpose:** fast triage — agents read this FIRST before opening any archive

Every cold archive has a corresponding manifest file stored outside the archive.

### Manifest Schema
```json
{
  "bundle_id": "",
  "division": "trading | opportunity | dev-automation | personal",
  "created_at": "<ISO timestamp>",
  "version": "1.0",
  "sensitivity": "low | medium | high",
  "files": [
    {
      "path": "",
      "hash": "",
      "size_bytes": 0,
      "tags": []
    }
  ],
  "summary": "",
  "extraction_hints": [],
  "ttl_hours": 24
}
```

**Field notes:**
- `bundle_id` — matches the archive filename (without date/extension)
- `sensitivity` — governs whether content can be referenced in Telegram messages
- `extraction_hints` — file paths the division chief should extract for common tasks
- `ttl_hours` — how long extracted files stay in hot cache before eviction

Rules:
- Manifest must be written when the archive is created
- Manifest must be updated if the archive is replaced
- Manifests are never compressed — they must be readable without extraction
- If manifest is missing for an existing archive: rebuild it before using the archive

---

## State 3 — Hot Extracted Cache

**Location:** `divisions/{name}/hot/`
**Purpose:** active working set — where division orchestrators actually operate

Rules:
- Extract only the files needed for the current task (use `extraction_hints` as guide)
- Never extract the full archive unless explicitly required
- Cache extracted files with TTL from manifest (default 24h)
- Index extracted files immediately after extraction
- Do NOT rezip hot files after every task — leave them cached
- Rezip only at checkpoint boundaries (end-of-day, explicit archive command, or when TTL expires)
- Hot directory size limit: governed by `artifact_policy.max_hot_mb` in division config

### Hot Index Entry (written to `divisions/{name}/index/`)
```json
{
  "bundle_id": "",
  "extracted_at": "<ISO timestamp>",
  "expires_at": "<ISO timestamp>",
  "files": [
    {
      "path": "",
      "size_bytes": 0,
      "summary": "",
      "embedding_ref": ""
    }
  ]
}
```

---

## State 4 — Executive Packet

**Location:** `divisions/{name}/packets/`
**Purpose:** the ONLY format delivered upward to J_Claw

Division orchestrators compile this after completing their skill run.
J_Claw reads packets — never hot files, never archives, never state JSON directly.

### Executive Packet Schema
```json
{
  "division": "trading | opportunity | dev-automation | personal",
  "generated_at": "<ISO timestamp>",
  "skill": "",
  "status": "success | partial | failed",
  "summary": "",
  "action_items": [
    {
      "priority": "high | medium | low",
      "description": "",
      "requires_matthew": true
    }
  ],
  "metrics": {},
  "artifact_refs": [
    {
      "bundle_id": "",
      "files": [],
      "location": "cold | hot"
    }
  ],
  "escalate": false,
  "escalation_reason": ""
}
```

**Field notes:**
- `action_items` — only include items that need attention; omit if empty
- `metrics` — division-specific KPIs (e.g., `win_rate`, `jobs_found`, `flags_raised`)
- `artifact_refs` — pointers to relevant archives/hot files; never inline file contents
- `escalate` — set true only if J_Claw must act immediately, not at next briefing
- `escalation_reason` — required if `escalate: true`

---

## Progression Packet (Realm Keeper → J_Claw)

Separate from division packets. Realm Keeper sends this after XP events.

```json
{
  "event": "xp_grant | reward | rank_up | achievement_unlock",
  "generated_at": "<ISO timestamp>",
  "xp_granted": 0,
  "xp_type": "base | division",
  "division": "",
  "new_total_xp": 0,
  "rank_up": false,
  "old_rank": "",
  "new_rank": "",
  "level": 0,
  "achievement_unlocked": "",
  "send_telegram": true,
  "message": ""
}
```

---

## Artifact Lifecycle Rules (summary)

| Rule | Policy |
|---|---|
| ZIP is for | Cold storage, distribution, snapshotting |
| ZIP is NOT for | Active inference, hot-path processing, repeated extract/rezip cycles |
| Model files | Raw GGUF only in `models/base/` — never compressed |
| LoRA adapters | Raw files in `models/adapters/` — hot-loaded, never compressed |
| Manifest first | Always read manifest before opening any archive |
| Extract minimum | Only files needed for the current task |
| Rezip when | Checkpoint boundary, end-of-day, or TTL expiry |
| Rezip never | After every task, after every read, on hot-path |
| J_Claw sees | Executive packets and progression packets only |

---

## Division Folder Layout

```
divisions/
  {name}/
    cold/          .zip / .7z archives
    manifests/     manifest.json files (one per archive)
    hot/           extracted working files (TTL-managed)
    index/         hot index entries + embedding refs
    packets/       executive_packet.json (latest per skill)
    config.json    division config (includes artifact_policy)
```

---

## Model Library Layout (on inference machine)

```
models/
  base/        Raw GGUF base model files — mmap-loaded, never compressed
  adapters/    LoRA adapter files — hot-loaded per division
  cold/        Rarely-used fine-tunes — archived when not active
  manifests/   Model manifests (size, hash, capabilities, adapter list)
```

Model manifest schema:
```json
{
  "model_id": "",
  "filename": "",
  "size_bytes": 0,
  "hash": "",
  "quantization": "Q4_K_M | Q5_K_M | Q8_0",
  "capabilities": [],
  "compatible_adapters": [],
  "mmap": true,
  "last_loaded": "<ISO timestamp>"
}
```
