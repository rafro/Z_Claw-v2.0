"""
Video Generator — submits AnimateDiff jobs to ComfyUI and retrieves results.
Falls back to queue-only behavior if ComfyUI is unavailable.

Saves outputs to mobile/assets/generated/videos/{commander}/

AMD/Windows notes
-----------------
* ComfyUI handles DirectML/RDNA 4 — no extra config needed here.
* Motion module: mm_sdxl_v10_beta.ckpt in ComfyUI/models/animatediff_models/
* Checkpoint: animagine-xl-3.1.safetensors (also used for images)

ComfyUI custom node required:
    cd ComfyUI/custom_nodes
    git clone https://github.com/Kosinkadink/ComfyUI-AnimateDiff-Evolved
    (restart ComfyUI after installing)

Download motion module from:
    https://huggingface.co/guoyww/animatediff/tree/main  ->  mm_sdxl_v10_beta.ckpt
"""

import json
import logging
import os
import shutil
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from runtime.config import BASE_DIR

log = logging.getLogger(__name__)

COMFYUI_URL         = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
OUTPUT_BASE         = BASE_DIR / "mobile" / "assets" / "generated" / "videos"
WORKFLOW_DIR        = BASE_DIR / "divisions" / "production" / "workflows"
QUEUE_FILE          = BASE_DIR / "state" / "video-queue.json"
_DIV_CONFIG_PATH    = BASE_DIR / "divisions" / "production" / "config.json"

# ComfyUI's local output directory — override via env if your path differs
COMFYUI_OUTPUT_DIR  = Path(os.getenv("COMFYUI_OUTPUT_DIR", "C:/Users/Tyler/ComfyUI/output"))


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------

def _load_queue() -> list:
    if not QUEUE_FILE.exists():
        return []
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_queue(queue: list) -> None:
    QUEUE_FILE.parent.mkdir(exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)


def _update_queue_status(entry_id: str, status: str, video_path: str = "") -> None:
    queue = _load_queue()
    for entry in queue:
        if entry.get("id") == entry_id:
            entry["status"]     = status
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            if video_path:
                entry["video_path"] = video_path
            break
    _save_queue(queue)


# ---------------------------------------------------------------------------
# Division config
# ---------------------------------------------------------------------------

def _load_division_config() -> dict:
    try:
        with open(_DIV_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("video_generate: could not load division config: %s", e)
        return {}


# ---------------------------------------------------------------------------
# ComfyUI availability check
# ---------------------------------------------------------------------------

def _comfyui_available() -> bool:
    delays = [0, 1, 3]
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=10)
            return True
        except Exception:
            if attempt < len(delays) - 1:
                log.warning("video_generate: ComfyUI check attempt %d failed, retrying...", attempt + 1)
    return False


# ---------------------------------------------------------------------------
# Workflow building
# ---------------------------------------------------------------------------

def _build_video_workflow(
    positive_prompt: str,
    negative_prompt: str,
    client_id: str,
    width: int = 512,
    height: int = 512,
    frames: int = 16,
    steps: int = 20,
    cfg: float = 7.0,
    seed: int | None = None,
) -> dict:
    """
    Build a ComfyUI /prompt payload for AnimateDiff.
    Loads video_animatediff.json then overrides prompts/dims/seed.
    Falls back to _inline_workflow() if JSON is missing.
    Scans by class_type so it's robust to key renaming.
    """
    workflow_path = WORKFLOW_DIR / "video_animatediff.json"
    if workflow_path.exists():
        with open(workflow_path, "r", encoding="utf-8") as f:
            graph: dict = json.load(f)
        # Strip _comment and _requires keys (not valid ComfyUI nodes)
        graph = {k: v for k, v in graph.items() if not k.startswith("_")}
    else:
        log.warning("video_generate: video_animatediff.json not found — using inline fallback")
        graph = _inline_workflow()

    division_cfg  = _load_division_config()
    ckpt_name     = division_cfg.get("comfyui_model",        "animagine-xl-3.1.safetensors")
    motion_module = division_cfg.get("comfyui_motion_module", "mm_sdxl_v10_beta.ckpt")

    if seed is None:
        seed = int(time.time()) % (2 ** 32)

    # Locate nodes by class_type
    checkpoint_key = context_key = loader_key = latent_key = ksampler_key = None
    clip_nodes: list[str] = []

    for key, node in graph.items():
        ct = node.get("class_type", "")
        if ct == "CheckpointLoaderSimple":
            checkpoint_key = key
        elif ct == "CLIPTextEncode":
            clip_nodes.append(key)
        elif ct == "KSampler":
            ksampler_key = key
        elif ct in ("EmptyLatentImage", "ADE_EmptyLatentImageLarge"):
            latent_key = key
        elif ct == "ADE_AnimateDiffUniformContextOptions":
            context_key = key
        elif ct == "ADE_AnimateDiffLoaderWithContext":
            loader_key = key

    clip_nodes.sort()

    if checkpoint_key:
        graph[checkpoint_key]["inputs"]["ckpt_name"] = ckpt_name
    if loader_key:
        graph[loader_key]["inputs"]["motion_model"]  = motion_module
        graph[loader_key]["inputs"]["beta_schedule"]  = "sqrt_linear (AnimateDiff)"
    if context_key:
        graph[context_key]["inputs"]["context_length"]  = frames
        graph[context_key]["inputs"]["context_stride"]  = 1
        graph[context_key]["inputs"]["context_overlap"] = 4
    if len(clip_nodes) >= 1:
        graph[clip_nodes[0]]["inputs"]["text"] = positive_prompt
    if len(clip_nodes) >= 2:
        graph[clip_nodes[1]]["inputs"]["text"] = negative_prompt
    if latent_key:
        graph[latent_key]["inputs"]["width"]      = width
        graph[latent_key]["inputs"]["height"]     = height
        graph[latent_key]["inputs"]["batch_size"] = frames
    if ksampler_key:
        graph[ksampler_key]["inputs"]["seed"]         = seed
        graph[ksampler_key]["inputs"]["steps"]        = steps
        graph[ksampler_key]["inputs"]["cfg"]          = cfg
        graph[ksampler_key]["inputs"]["sampler_name"] = "euler_ancestral"
        graph[ksampler_key]["inputs"]["scheduler"]    = "karras"
        graph[ksampler_key]["inputs"]["denoise"]      = 1.0

    return {"client_id": client_id, "prompt": graph}


def _inline_workflow() -> dict:
    """Minimal AnimateDiff-SDXL graph — emergency fallback only."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "animagine-xl-3.1.safetensors"}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "cinematic scene, masterpiece, best quality", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "worst quality, low quality, blurry, watermark", "clip": ["1", 1]}},
        "4": {"class_type": "ADE_AnimateDiffUniformContextOptions",
              "inputs": {"context_length": 16, "context_stride": 1, "context_overlap": 4, "closed_loop": False}},
        "5": {"class_type": "ADE_AnimateDiffLoaderWithContext",
              "inputs": {"model": ["1", 0], "context_options": ["4", 0],
                         "motion_model": "mm_sdxl_v10_beta.ckpt",
                         "beta_schedule": "sqrt_linear (AnimateDiff)",
                         "motion_scale": 1.0, "apply_v2_models_properly": True}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": 512, "height": 512, "batch_size": 16}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["5", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["6", 0], "seed": 0, "steps": 20, "cfg": 7.0,
                         "sampler_name": "euler_ancestral", "scheduler": "karras", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["1", 2]}},
        "9": {"class_type": "SaveAnimatedWEBP",
              "inputs": {"images": ["8", 0], "filename_prefix": "animatediff_",
                         "fps": 8.0, "lossless": False, "quality": 85, "method": "default"}},
    }


# ---------------------------------------------------------------------------
# Submit + poll
# ---------------------------------------------------------------------------

def _submit_and_wait(workflow: dict, timeout_s: int = 600) -> dict:
    data = json.dumps(workflow).encode("utf-8")
    req  = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())

    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI rejected prompt: {result}")

    log.info("video_generate: prompt_id=%s — polling for completion", prompt_id)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(5)
        try:
            with urllib.request.urlopen(f"{COMFYUI_URL}/history/{prompt_id}", timeout=10) as r:
                history = json.loads(r.read())
            if prompt_id in history:
                job    = history[prompt_id]
                status = job.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI reported error: {status.get('messages', [])}")

                outputs: list[str] = []
                for node_out in job.get("outputs", {}).values():
                    for img in node_out.get("images", []):
                        outputs.append(img["filename"])
                    for gif in node_out.get("gifs", []):
                        outputs.append(gif["filename"])
                return {"prompt_id": prompt_id, "filenames": outputs}
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            log.debug("video_generate: poll error: %s", e)

    raise TimeoutError(f"ComfyUI video generation timed out after {timeout_s}s")


def _copy_output(filename: str, out_dir: Path) -> Path:
    """Copy a ComfyUI output file to the asset directory using the local filesystem."""
    src = COMFYUI_OUTPUT_DIR / filename
    if not src.exists():
        candidates = list(COMFYUI_OUTPUT_DIR.rglob(filename))
        if candidates:
            src = candidates[0]
        else:
            raise FileNotFoundError(
                f"ComfyUI output file not found: {filename} (searched {COMFYUI_OUTPUT_DIR})"
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / filename
    shutil.copy2(src, dest)
    log.info("video_generate: copied %s -> %s", src, dest)
    return dest


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(
    scene_type:  str   = "battle",
    commander:   str   = "generic",
    description: str   = "",
    width:       int   = 512,
    height:      int   = 512,
    frames:      int   = 16,
    steps:       int   = 20,
    cfg:         float = 7.0,
) -> dict:
    """
    Video Generator skill entry point.

    When ComfyUI + AnimateDiff-Evolved are running: submits an AnimateDiff
    generation job, waits for completion, copies the output WEBP to
    mobile/assets/generated/videos/{commander}/.
    When ComfyUI is offline: queues the request for later processing.
    """
    queue      = _load_queue()
    entry_id   = f"vid-{len(queue)+1:04d}"
    description = description or f"{commander} {scene_type} scene"

    entry = {
        "id":          entry_id,
        "scene_type":  scene_type,
        "commander":   commander,
        "description": description,
        "status":      "queued",
        "queued_at":   datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)
    _save_queue(queue)
    log.info("video_generate: queued %s / %s (%s)", scene_type, commander, entry_id)

    if not _comfyui_available():
        log.warning("video_generate: ComfyUI not reachable — queue-only mode")
        return {
            "status":  "partial",
            "summary": (
                f"Video request queued ({entry_id}). "
                "ComfyUI is offline — start ComfyUI and re-run to generate."
            ),
            "metrics": {
                "queue_id":       entry_id,
                "scene_type":     scene_type,
                "commander":      commander,
                "queue_depth":    len(queue),
                "comfyui_online": False,
            },
            "action_items": [{
                "priority":         "high",
                "description":      "Launch ComfyUI (run_amd_gpu.bat), then retry video-generate.",
                "requires_matthew": True,
            }],
            "escalate": False,
        }

    try:
        positive_prompt = (
            f"{description}, cinematic video, smooth animation, "
            "masterpiece, best quality, highly detailed, 8fps"
        )
        negative_prompt = (
            "worst quality, low quality, blurry, static, frozen frame, "
            "watermark, text, nsfw, deformed"
        )

        client_id = str(uuid.uuid4())
        workflow  = _build_video_workflow(
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            client_id=client_id,
            width=width,
            height=height,
            frames=frames,
            steps=steps,
            cfg=cfg,
        )

        log.info("video_generate: submitting AnimateDiff job — %s/%s %dx%d %df", scene_type, commander, width, height, frames)
        result    = _submit_and_wait(workflow, timeout_s=600)
        filenames = result.get("filenames", [])

        out_dir = OUTPUT_BASE / commander
        copied: list[str] = []
        for fname in filenames:
            try:
                dest = _copy_output(fname, out_dir)
                copied.append(str(dest))
            except FileNotFoundError as e:
                log.warning("video_generate: %s", e)

        video_path = copied[0] if copied else ""
        _update_queue_status(entry_id, "completed", video_path)

        return {
            "status":  "success",
            "summary": f"Generated {len(filenames)} video file(s) for {commander} ({scene_type}, {frames} frames).",
            "metrics": {
                "queue_id":       entry_id,
                "scene_type":     scene_type,
                "commander":      commander,
                "frames":         frames,
                "prompt_id":      result.get("prompt_id"),
                "filenames":      filenames,
                "video_path":     video_path,
                "copied_paths":   copied,
                "comfyui_online": True,
            },
            "action_items": [],
            "escalate": False,
        }

    except Exception as exc:
        log.error("video_generate: generation failed — %s", exc)
        _update_queue_status(entry_id, "failed")
        return {
            "status":  "failed",
            "summary": f"Video generation failed: {exc}",
            "metrics": {
                "queue_id":       entry_id,
                "commander":      commander,
                "scene_type":     scene_type,
                "comfyui_online": True,
            },
            "action_items": [{"priority": "high", "description": str(exc), "requires_matthew": False}],
            "escalate": True,
        }
