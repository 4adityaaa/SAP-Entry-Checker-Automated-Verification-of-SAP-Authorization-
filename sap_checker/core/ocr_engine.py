"""OCR + OpenCV image preparation.

Pipeline per screenshot:
  1. load (EXIF rotation fixed), limit size
  2. OCR (RapidOCR / ONNX - pure pip install, no Tesseract needed)
  3. measure skew from the text boxes; if tilted (photo of a monitor) rotate
     with OpenCV and OCR again
"""
import ctypes
import io
import os
import sys
import threading
from typing import List, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 300_000_000

OcrItem = Tuple[np.ndarray, str, float]     # (4x2 box, text, score)

_engine = None
_engine_name = ""
_lock = threading.Lock()
_native_runtime_handles = []


def _prepare_windows_native_runtime() -> None:
    """Load app-local MSVC runtime DLLs before native OCR extensions."""
    if os.name != "nt":
        return

    directories = (sys.prefix, os.path.join(sys.prefix, "Scripts"))
    for name in ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
                 "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll"):
        for directory in directories:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                try:
                    _native_runtime_handles.append(ctypes.WinDLL(path))
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not load the Windows C++ runtime {name}: {exc}. "
                        "Reinstall project dependencies with "
                        "`python -m pip install -r requirements.txt`.") from exc
                break


def _build_engine():
    """Supports both the classic `rapidocr_onnxruntime` and the newer `rapidocr`."""
    _prepare_windows_native_runtime()
    try:
        from rapidocr_onnxruntime import RapidOCR as _R1

        eng = _R1()

        def run(img):
            res, _ = eng(img)
            return [(np.array(b, dtype=np.float32), str(t), float(s)) for b, t, s in (res or [])]

        return run, "rapidocr-onnxruntime"
    except ModuleNotFoundError as exc:
        if exc.name != "rapidocr_onnxruntime":
            raise RuntimeError(
                "RapidOCR's `rapidocr-onnxruntime` package is installed but a dependency "
                f"could not be loaded: {type(exc).__name__}: {exc}. "
                "Use Python 3.10-3.12 and reinstall with "
                "`python -m pip install -r requirements.txt`.") from exc
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "RapidOCR's `rapidocr-onnxruntime` package is installed but could not be "
            f"initialized: {type(exc).__name__}: {exc}. "
            "Use Python 3.10-3.12 and reinstall with "
            "`python -m pip install -r requirements.txt`.") from exc

    try:
        from rapidocr import RapidOCR as _R2

        eng = _R2()

        def run(img):
            r = eng(img)
            if r is None or getattr(r, "boxes", None) is None:
                return []
            return [(np.array(b, dtype=np.float32), str(t), float(s))
                    for b, t, s in zip(r.boxes, r.txts, r.scores)]

        return run, "rapidocr"
    except ModuleNotFoundError as exc:
        if exc.name != "rapidocr":
            raise RuntimeError(
                "RapidOCR's `rapidocr` package is installed but a dependency could not "
                f"be loaded: {type(exc).__name__}: {exc}. "
                "Use Python 3.10-3.12 and reinstall with "
                "`python -m pip install -r requirements.txt`.") from exc
        raise RuntimeError(
            "No RapidOCR package is installed in this Python environment. "
            "Use Python 3.10-3.12, then run "
            "`python -m pip install -r requirements.txt` with the same Python "
            "environment used to start the app.") from exc
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "RapidOCR's `rapidocr` package is installed but could not be initialized: "
            f"{type(exc).__name__}: {exc}. "
            "Use Python 3.10-3.12 and reinstall with "
            "`python -m pip install -r requirements.txt`.") from exc


def run_ocr(img_bgr: np.ndarray) -> List[OcrItem]:
    global _engine, _engine_name
    with _lock:
        if _engine is None:
            _engine, _engine_name = _build_engine()
        return _engine(img_bgr)


def engine_name() -> str:
    return _engine_name or "not loaded yet"


# ----------------------------------------------------------------------------
def load_image(data: bytes, max_side: int = 2000, min_side: int = 1300) -> np.ndarray:
    """bytes -> BGR ndarray. Fixes phone EXIF rotation, caps / raises size."""
    pil = Image.open(io.BytesIO(data))
    pil = ImageOps.exif_transpose(pil).convert("RGB")
    img = np.array(pil)[:, :, ::-1].copy()
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > max_side:
        scale = max_side / long_side
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    elif long_side < min_side:
        scale = min(min_side / long_side, 3.0)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    return img


def blur_score(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def enhance(img: np.ndarray) -> np.ndarray:
    """Only sharpen soft/blurry images. (CLAHE was tested and it erased
    highlighted, selected cells like the blue '07', so it is NOT used.)"""
    if blur_score(img) < 80:
        soft = cv2.GaussianBlur(img, (0, 0), 2.0)
        return cv2.addWeighted(img, 1.8, soft, -0.8, 0)
    return img


def estimate_skew(items: List[OcrItem]) -> float:
    """Median angle (degrees) of long text boxes. Positive = clockwise tilt."""
    angles = []
    for box, text, score in items:
        w = float(np.linalg.norm(box[1] - box[0]))
        h = float(np.linalg.norm(box[3] - box[0]))
        if h > 0 and w > 2.5 * h and len(text) >= 4 and score > 0.6:
            angles.append(np.degrees(np.arctan2(box[1][1] - box[0][1], box[1][0] - box[0][0])))
    if len(angles) < 4:
        return 0.0
    return float(np.median(angles))


def rotate(img: np.ndarray, angle: float) -> np.ndarray:
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def read_screenshot(data: bytes):
    """Returns (image_used, ocr_items, info_dict)."""
    img = load_image(data)
    info = {"blur": round(blur_score(img), 1), "width": img.shape[1], "height": img.shape[0],
            "skew": 0.0, "warnings": []}
    work = enhance(img)
    items = run_ocr(work)

    skew = estimate_skew(items)
    if 0.6 <= abs(skew) <= 20:
        work = rotate(work, skew)
        img = rotate(img, skew)
        items = run_ocr(work)
        info["skew"] = round(skew, 2)

    if info["blur"] < 40:
        info["warnings"].append("Image looks blurry - results may be unreliable.")
    if min(info["width"], info["height"]) < 500:
        info["warnings"].append("Low resolution image.")
    if len(items) < 8:
        info["warnings"].append("Very little text detected - is this a SAP screen?")
    return img, items, info
