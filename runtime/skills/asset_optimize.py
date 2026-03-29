"""
Asset Optimizer — upscales generated images and converts to optimized web formats.

Primary path: submits an upscale workflow to ComfyUI (RealESRGAN_x4plus).
Fallback path: PIL/Pillow LANCZOS resize when ComfyUI is unavailable.

Also handles PNG -> WebP conversion and EXIF metadata stripping for
smaller, privacy-safe assets suitable for mobile PWA delivery.

Outputs to mobile/assets/generated/optimized/{original_stem}_{scale}x.{format}
"""

import json
import logging
import os
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

from runtime.config import BASE_DIR

log = logging.getLogger(__name__)

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
OUTPUT_BASE = BASE_DIR / "mobile" / "assets" / "generated" / "optimized"


# ---------------------------------------------------------------------------
# ComfyUI output directory resolution (mirrors video_generate pattern)
# ---------------------------------------------------------------------------

def _resolve_comfyui_output_dir() -> Path:
    """Resolve ComfyUI output directory — env var, config, or common fallbacks."""
    env_val = os.getenv("COMFYUI_OUTPUT_DIR")
    if env_val:
        return Path(env_val)

    config_derived: Path | None = None
    try:
        cfg_path = BASE_DIR / "divisions" / "production" / "config.json"
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        bat_path = cfg.get("comfyui_path", "")
        if bat_path:
            config_derived = Path(bat_path).parent / "output"
    except Exception:
        pass

    candidates: list[Path] = []
    if config_derived:
        candidates.append(config_derived)
    candidates.extend([
        Path("C:/ComfyUI/output"),
        Path("C:/Users/Tyler/ComfyUI/output"),
        Path.home() / "ComfyUI" / "output",
    ])

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    return config_derived if config_derived else Path("C:/Users/Tyler/ComfyUI/output")


COMFYUI_OUTPUT_DIR = _resolve_comfyui_output_dir()


# ---------------------------------------------------------------------------
# ComfyUI availability check
# ---------------------------------------------------------------------------

def _comfyui_available() -> bool:
    """Check if ComfyUI is reachable at /system_stats with a single fast attempt."""
    try:
        urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ComfyUI upscale workflow
# ---------------------------------------------------------------------------

def _build_upscale_workflow(image_filename: str, client_id: str) -> dict:
    """
    Build a ComfyUI /prompt payload for image upscaling.

    Workflow: LoadImage -> ImageUpscaleWithModel (RealESRGAN_x4plus) -> SaveImage

    The RealESRGAN_x4plus model provides 4x upscaling.  Scale factors other
    than 4x are handled by a subsequent PIL resize after ComfyUI returns.
    """
    graph = {
        "1": {
            "class_type": "LoadImage",
            "inputs": {
                "image": image_filename,
            },
        },
        "2": {
            "class_type": "UpscaleModelLoader",
            "inputs": {
                "model_name": "RealESRGAN_x4plus.pth",
            },
        },
        "3": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {
                "upscale_model": ["2", 0],
                "image": ["1", 0],
            },
        },
        "4": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["3", 0],
                "filename_prefix": "upscaled_",
            },
        },
    }
    return {"client_id": client_id, "prompt": graph}


# ---------------------------------------------------------------------------
# Submit + poll (mirrors video_generate._submit_and_wait)
# ---------------------------------------------------------------------------

def _submit_and_wait(workflow: dict, timeout_s: int = 300) -> dict:
    """Submit a workflow to ComfyUI and poll until complete."""
    data = json.dumps(workflow).encode("utf-8")
    req = urllib.request.Request(
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

    log.info("asset_optimize: prompt_id=%s — polling for completion", prompt_id)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(f"{COMFYUI_URL}/history/{prompt_id}", timeout=10) as r:
                history = json.loads(r.read())
            if prompt_id in history:
                job = history[prompt_id]
                status = job.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI reported error: {status.get('messages', [])}")

                outputs: list[str] = []
                for node_out in job.get("outputs", {}).values():
                    for img in node_out.get("images", []):
                        outputs.append(img["filename"])
                return {"prompt_id": prompt_id, "filenames": outputs}
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            log.debug("asset_optimize: poll error: %s", e)

    raise TimeoutError(f"ComfyUI upscale timed out after {timeout_s}s")


def _copy_comfyui_output(filename: str) -> Path:
    """Copy a ComfyUI output file to a temp location and return its path."""
    if not COMFYUI_OUTPUT_DIR.is_dir():
        raise FileNotFoundError(f"ComfyUI output directory not found: {COMFYUI_OUTPUT_DIR}")

    src = COMFYUI_OUTPUT_DIR / filename
    if not src.exists():
        candidates = list(COMFYUI_OUTPUT_DIR.rglob(filename))
        if candidates:
            src = candidates[0]
        else:
            raise FileNotFoundError(f"ComfyUI output file not found: {filename} (searched {COMFYUI_OUTPUT_DIR})")
    return src


# ---------------------------------------------------------------------------
# Upload input image to ComfyUI (required for LoadImage node)
# ---------------------------------------------------------------------------

def _upload_to_comfyui(image_path: Path) -> str:
    """
    Upload an image to ComfyUI's /upload/image endpoint so the LoadImage
    node can reference it by filename.  Returns the filename as stored by
    ComfyUI.
    """
    import mimetypes

    mime, _ = mimetypes.guess_type(str(image_path))
    if not mime:
        mime = "image/png"

    boundary = uuid.uuid4().hex
    filename = image_path.name

    with open(image_path, "rb") as f:
        file_data = f.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        f"{COMFYUI_URL}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    uploaded_name = result.get("name", filename)
    log.info("asset_optimize: uploaded %s to ComfyUI as %s", filename, uploaded_name)
    return uploaded_name


# ---------------------------------------------------------------------------
# PIL fallback upscale
# ---------------------------------------------------------------------------

def _pillow_upscale(image_path: Path, scale: int, out_format: str, quality: int) -> Path:
    """
    Upscale using PIL/Pillow LANCZOS resampling.  Also converts to the
    target format and strips EXIF metadata.
    """
    from PIL import Image

    img = Image.open(image_path)

    # Strip EXIF by re-creating without info dict
    data = list(img.getdata())
    clean = Image.new(img.mode, img.size)
    clean.putdata(data)

    new_w = clean.width * scale
    new_h = clean.height * scale
    upscaled = clean.resize((new_w, new_h), Image.LANCZOS)

    ext = "webp" if out_format.lower() == "webp" else out_format.lower()
    stem = image_path.stem
    out_name = f"{stem}_{scale}x.{ext}"
    out_path = OUTPUT_BASE / out_name
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    save_kwargs: dict = {}
    if ext == "webp":
        save_kwargs["quality"] = quality
        save_kwargs["method"] = 6  # best compression
    elif ext in ("jpg", "jpeg"):
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    elif ext == "png":
        save_kwargs["optimize"] = True

    upscaled.save(out_path, **save_kwargs)
    log.info("asset_optimize: PIL upscaled %s -> %s (%dx%d)", image_path.name, out_path.name, new_w, new_h)
    return out_path


# ---------------------------------------------------------------------------
# Post-process: convert format + strip EXIF on ComfyUI output
# ---------------------------------------------------------------------------

def _postprocess(src_path: Path, original_stem: str, scale: int, out_format: str, quality: int, target_w: int | None = None, target_h: int | None = None) -> Path:
    """
    Convert a ComfyUI upscaled image to the target format, optionally resize
    to exact target dimensions (when scale != 4), and strip EXIF.
    """
    from PIL import Image

    img = Image.open(src_path)

    # Strip EXIF
    data = list(img.getdata())
    clean = Image.new(img.mode, img.size)
    clean.putdata(data)

    # Resize to exact target if ComfyUI gave us 4x but we need a different scale
    if target_w and target_h and (clean.width != target_w or clean.height != target_h):
        clean = clean.resize((target_w, target_h), Image.LANCZOS)

    ext = "webp" if out_format.lower() == "webp" else out_format.lower()
    out_name = f"{original_stem}_{scale}x.{ext}"
    out_path = OUTPUT_BASE / out_name
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

    save_kwargs: dict = {}
    if ext == "webp":
        save_kwargs["quality"] = quality
        save_kwargs["method"] = 6
    elif ext in ("jpg", "jpeg"):
        save_kwargs["quality"] = quality
        save_kwargs["optimize"] = True
    elif ext == "png":
        save_kwargs["optimize"] = True

    clean.save(out_path, **save_kwargs)
    return out_path


# ---------------------------------------------------------------------------
# Skill entry point
# ---------------------------------------------------------------------------

def run(
    image_path: str = "",
    scale: int = 2,
    format: str = "webp",
    quality: int = 85,
) -> dict:
    """
    Asset Optimizer skill entry point.

    Upscales the input image by the given scale factor using ComfyUI
    (RealESRGAN_x4plus) when available, falling back to PIL/Pillow LANCZOS.
    Converts to the target format (default WebP) and strips EXIF metadata.

    Parameters
    ----------
    image_path : str
        Path to the source image file.
    scale : int
        Upscale factor (default 2).  ComfyUI always does 4x internally;
        if scale != 4 the output is resized to the exact target.
    format : str
        Output format — "webp" (default), "png", or "jpg".
    quality : int
        Compression quality for lossy formats (default 85).

    Returns
    -------
    dict with status, output_path, original_size, output_size, method,
    compression_ratio, metrics.
    """
    if not image_path:
        return {
            "status":  "partial",
            "summary": "No image path provided. Pass image_path=<path> to run asset optimization.",
            "output_path": "",
            "original_size": 0,
            "output_size": 0,
            "method": "none",
            "compression_ratio": 0.0,
            "metrics": {},
            "action_items": [],
            "escalate": False,
        }

    src = Path(image_path)
    if not src.exists():
        return {
            "status":  "failed",
            "summary": f"Source image not found: {image_path}",
            "output_path": "",
            "original_size": 0,
            "output_size": 0,
            "method": "none",
            "compression_ratio": 0.0,
            "metrics": {},
            "action_items": [{"priority": "normal", "description": f"File missing: {image_path}", "requires_matthew": False}],
            "escalate": False,
        }

    original_size = src.stat().st_size

    # Try to get source dimensions
    src_w, src_h = 0, 0
    try:
        from PIL import Image as _PILImg
        with _PILImg.open(src) as _tmp:
            src_w, src_h = _tmp.size
    except Exception:
        pass

    target_w = src_w * scale if src_w else None
    target_h = src_h * scale if src_h else None
    method = "none"
    out_path: Path | None = None

    # ── Primary path: ComfyUI upscale ────────────────────────────────────────
    if _comfyui_available():
        try:
            log.info("asset_optimize: ComfyUI available — submitting upscale job for %s", src.name)
            uploaded_name = _upload_to_comfyui(src)

            client_id = str(uuid.uuid4())
            workflow = _build_upscale_workflow(uploaded_name, client_id)
            result = _submit_and_wait(workflow, timeout_s=300)

            filenames = result.get("filenames", [])
            if filenames:
                comfyui_out = _copy_comfyui_output(filenames[0])
                out_path = _postprocess(
                    comfyui_out, src.stem, scale, format, quality,
                    target_w, target_h,
                )
                method = "comfyui"
                log.info("asset_optimize: ComfyUI upscale complete — %s", out_path.name)
            else:
                log.warning("asset_optimize: ComfyUI returned no output files — falling back to PIL")

        except Exception as exc:
            log.warning("asset_optimize: ComfyUI upscale failed (%s) — falling back to PIL", exc)

    # ── Fallback path: PIL/Pillow upscale ────────────────────────────────────
    if out_path is None:
        try:
            out_path = _pillow_upscale(src, scale, format, quality)
            method = "pillow"
        except ImportError:
            return {
                "status":  "failed",
                "summary": "Neither ComfyUI nor Pillow available — cannot upscale.",
                "output_path": "",
                "original_size": original_size,
                "output_size": 0,
                "method": "none",
                "compression_ratio": 0.0,
                "metrics": {},
                "action_items": [{"priority": "high", "description": "Install Pillow: pip install Pillow", "requires_matthew": False}],
                "escalate": True,
            }
        except Exception as exc:
            return {
                "status":  "failed",
                "summary": f"PIL upscale failed: {exc}",
                "output_path": "",
                "original_size": original_size,
                "output_size": 0,
                "method": "pillow",
                "compression_ratio": 0.0,
                "metrics": {"error": str(exc)},
                "action_items": [{"priority": "high", "description": str(exc), "requires_matthew": False}],
                "escalate": True,
            }

    # ── Build result ─────────────────────────────────────────────────────────
    output_size = out_path.stat().st_size
    compression_ratio = round(output_size / original_size, 2) if original_size > 0 else 0.0

    # Determine output dimensions
    out_w, out_h = 0, 0
    try:
        from PIL import Image as _PILImg2
        with _PILImg2.open(out_path) as _tmp2:
            out_w, out_h = _tmp2.size
    except Exception:
        pass

    summary = (
        f"Upscaled {src.name} {scale}x via {method} -> {out_path.name}. "
        f"{src_w}x{src_h} -> {out_w}x{out_h}. "
        f"Size: {original_size:,}B -> {output_size:,}B (ratio {compression_ratio}x)."
    )

    log.info("asset_optimize: %s", summary)

    return {
        "status":  "success",
        "summary": summary,
        "output_path": str(out_path),
        "original_size": original_size,
        "output_size": output_size,
        "method": method,
        "compression_ratio": compression_ratio,
        "metrics": {
            "source":           str(src),
            "output":           str(out_path),
            "scale":            scale,
            "format":           format,
            "quality":          quality,
            "method":           method,
            "original_dims":    f"{src_w}x{src_h}",
            "output_dims":      f"{out_w}x{out_h}",
            "original_bytes":   original_size,
            "output_bytes":     output_size,
            "compression_ratio": compression_ratio,
        },
        "action_items": [],
        "escalate": False,
    }
