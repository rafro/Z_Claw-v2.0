"""Atomic JSON write — tempfile + os.replace to prevent corruption."""
import json, os, tempfile
from pathlib import Path

def atomic_write_json(path: Path, data, **kwargs) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, default=str, **kwargs)
        os.replace(tmp, str(path))
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
