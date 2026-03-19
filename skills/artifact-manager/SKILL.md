---
name: artifact-manager
description: Shared utility used by all division orchestrators. Manages the cold/manifest/hot/index artifact lifecycle. Handles archive inspection, selective extraction, TTL-based eviction, checkpoint archiving, and hot cache indexing. Never called by J_Claw directly.
division: all
trigger: called by division orchestrators as a utility
---

## Role
The Artifact Manager is a shared infrastructure utility. It is not a skill that
produces results for J_Claw. It is called by division orchestrators to manage
their artifact lifecycle operations.

Division orchestrators call these operations. J_Claw never calls artifact-manager directly.

---

## Operations

### `inspect_manifest(bundle_id, division)`
Read the manifest for a bundle WITHOUT opening the archive.

Steps:
1. Resolve manifest path: `divisions/{division}/manifests/{bundle_id}.manifest.json`
2. Read and parse the manifest JSON
3. Return: file list, tags, summary, extraction_hints, sensitivity, ttl_hours
4. If manifest missing: return error — do not attempt to open archive

Use case: division chief decides which files to extract before touching the archive.

---

### `hydrate(bundle_id, division, file_list)`
Extract specific files from a cold archive into the hot cache.

Steps:
1. Call `inspect_manifest()` first — verify bundle exists and is readable
2. Resolve archive path: `divisions/{division}/cold/{bundle_id}_*.zip`
3. Extract ONLY the files listed in `file_list` (not full archive)
4. Write extracted files to: `divisions/{division}/hot/{bundle_id}/`
5. Record extraction in hot index: `divisions/{division}/index/{bundle_id}.index.json`
6. Set expiry timestamp: `now + manifest.ttl_hours`

Rules:
- Never extract the full archive unless `file_list` is explicitly `["*"]`
- If a file in `file_list` does not exist in the archive: log warning, skip it, continue
- Never decompress model files (GGUF, .bin) — they live in `models/` as raw files

---

### `cache_ttl_check(division)`
Evict expired files from the hot cache.

Steps:
1. Read all index entries in `divisions/{division}/index/`
2. For each entry: check if `expires_at` < now
3. If expired:
   - If `pinned: true`: skip (do not evict pinned entries)
   - Otherwise: delete files from `divisions/{division}/hot/{bundle_id}/`
   - Mark index entry as evicted
4. If hot directory exceeds `artifact_policy.max_hot_mb` from config:
   - Evict oldest non-pinned entries until under limit

Call at the start of every division task run.

---

### `archive(source_path, division, bundle_id, tags)`
Rezip hot files into a cold archive at checkpoint boundaries.

Steps:
1. Verify `source_path` exists in `divisions/{division}/hot/`
2. Create archive: `divisions/{division}/cold/{bundle_id}_{YYYY-MM-DD}.zip`
3. Write all files from `source_path` into the archive
4. Generate or update manifest: `divisions/{division}/manifests/{bundle_id}.manifest.json`
   - Compute file hashes
   - Set `created_at`, `version`, `tags`, `sensitivity`
   - Write `extraction_hints` based on file types present
5. Remove source files from hot cache after successful archive
6. Update hot index to mark bundle as archived

Rules:
- Only call at checkpoint boundaries — NOT after every task
- Never archive open/in-progress task files
- Never archive raw model files (GGUF, .bin, .gguf)
- Verify archive integrity before removing hot files

---

### `index_extracted(bundle_id, division)`
Generate a lightweight index entry for recently extracted/created hot files.

Steps:
1. Scan `divisions/{division}/hot/{bundle_id}/` for all files
2. For each file:
   - Record: path, size_bytes, last_modified
   - Generate short summary (file type + content description — local model call)
   - If embeddings enabled: generate embedding hash, store ref
3. Write index entry to `divisions/{division}/index/{bundle_id}.index.json`
4. Set `extracted_at` and `expires_at` fields

---

## Model Loading Policy

This section applies to division orchestrators when loading their GGUF models.

**DO:**
- Load GGUF files using mmap (memory-mapped I/O)
- Keep model resident between tasks in the same session
- Load LoRA adapters from `models/adapters/` as needed per task
- Cache adapter merges in memory for the session duration

**DO NOT:**
- Load a model from a compressed archive (.zip, .7z)
- Reload the model from disk between tasks in the same session
- Compress active model files
- Inflate a compressed model file at runtime

**Loading sequence:**
1. Check if model is already loaded in memory → reuse
2. If not loaded: read model path from division config → `model.path`
3. Open GGUF file with mmap flag enabled
4. Load LoRA adapter if division config specifies one → `model.adapter`
5. Model stays loaded until session ends or explicit unload

---

## Error Handling
- `inspect_manifest` fails (missing manifest): return structured error, do not open archive
- `hydrate` fails (corrupt archive): return error to division chief, do not partially extract
- `cache_ttl_check` fails: log warning, continue — stale cache is preferable to a crash
- `archive` fails (disk full, permission error): escalate to division chief → packet to J_Claw
- Never silently discard files during eviction — always log what was removed and why
