"""Long-lived VSR worker for VideoSearch pipeline (JSON lines on stdin).

One Python process loads Torch/Paddle once per pipeline run; each line is one clip job.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
import traceback
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _configure_stdio() -> None:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
    os.environ.setdefault("PYTHONUTF8", "1")


def _apply_job_config(job: dict) -> None:
    from backend.config import config
    from backend.tools.constant import InpaintMode, SubtitleDetectMode

    mode_s = str(job.get("inpaint_mode", "sttn-auto")).strip().lower().replace("_", "-")
    detect_s = str(job.get("subtitle_detect_mode", "precise")).strip().lower()
    config.inpaintMode.value = InpaintMode[mode_s.replace("-", "_").upper()]
    if detect_s in ("fast", "mobile"):
        config.set(config.subtitleDetectMode, SubtitleDetectMode.PP_OCRv5_MOBILE)
    else:
        config.set(config.subtitleDetectMode, SubtitleDetectMode.PP_OCRv5_SERVER)


def _warmup_once() -> None:
    """Pay torch/CUDA import cost once per worker process."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.init()
    except Exception:
        pass


_WARMED = False

# Parent reads only this stream for JSON replies (see video_workflow.vsr_subprocess).
_JSON_OUT = sys.stdout


def _emit_json(payload: dict) -> None:
    _JSON_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _JSON_OUT.flush()


def _process_job(job: dict) -> None:
    global _WARMED
    from backend.main import SubtitleRemover

    if not _WARMED:
        _warmup_once()
        _WARMED = True

    _apply_job_config(job)
    inp = str(job["input"])
    outp = str(job["output"])
    areas = job.get("subtitle_areas") or []

    # tqdm in VSR uses sys.__stdout__, not sys.stdout — redirect both to stderr.
    prev_stdout = sys.stdout
    prev__stdout__ = sys.__stdout__
    sys.stdout = sys.stderr
    sys.__stdout__ = sys.stderr
    t0 = time.monotonic()
    try:
        sr = SubtitleRemover(inp, gui_mode=False)
        sr.sub_areas = [tuple(int(x) for x in quad) for quad in areas]
        sr.video_out_path = outp
        sr.run()
    finally:
        sys.stdout = prev_stdout
        sys.__stdout__ = prev__stdout__
    out_path = Path(outp)
    if not out_path.is_file() or out_path.stat().st_size < 4096:
        raise RuntimeError(f"VSR 未生成有效输出：{outp}")
    _emit_json(
        {
            "ok": True,
            "duration_sec": round(time.monotonic() - t0, 3),
            "frame_count": int(getattr(sr, "frame_count", 0) or 0),
            "output": outp,
        }
    )


def main() -> int:
    multiprocessing.set_start_method("spawn", force=True)
    _configure_stdio()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit_json({"ok": False, "error": f"invalid json: {exc}"})
            continue
        if job.get("cmd") == "quit":
            _emit_json({"ok": True})
            break
        try:
            _process_job(job)
        except Exception as exc:
            traceback.print_exc()
            _emit_json(
                {"ok": False, "error": str(exc), "traceback": traceback.format_exc()[-4000:]},
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
