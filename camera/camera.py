#!/usr/bin/env python3
"""
Camera manager for MeshCenter.

This module contains all Raspberry Pi camera, MJPEG video, photo capture,
settings and screenshot gallery logic. Flask routes stay in server.py.
"""

import base64
import io
import json
import os
import threading
import time
from datetime import datetime
from PIL import Image

# libcamera is only present on real CSI-camera hardware (Picamera2's own
# dependency) - devices running the USB driver instead (camera/usb_driver.py,
# no libcamera dependency at all) or with no camera hardware at all must
# still be able to import this module, since server.py imports it
# unconditionally at startup (see camera/csi_driver.py's own import of this
# module too). This is a different failure mode than "camera hardware not
# detected" (already handled gracefully at runtime by init_camera()'s own
# try/except around Picamera2() below) - an ImportError here happens before
# init_camera() ever runs, so it needs its own guard.
try:
    from libcamera import Transform, controls as libcamera_controls
    LIBCAMERA_AVAILABLE = True
except ImportError:
    Transform = None
    libcamera_controls = None
    LIBCAMERA_AVAILABLE = False

try:
    from config import DATA_DIR
except ImportError:
    DATA_DIR = "data"

# ============================================================
# CAMERA PATHS
# ============================================================

SCREENSHOTS_DIR = os.path.join(DATA_DIR, "screenshots")
CAMERA_CONFIG_FILE = os.path.join(DATA_DIR, "camera_config.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# ============================================================
# CAMERA CONFIG
# ============================================================

VIDEO_CONFIG = {
    "resolution": "640x480",
    "fps": 12,
    "quality": 75,
}

CAMERA_CONTROLS = {
    # White balance
    "awb_mode": "auto",
    "red_gain": 1.0,
    "blue_gain": 1.0,

    # Image adjustment
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "sharpness": 1.0,

    # Exposure
    "exposure_compensation": 0.0,
    "exposure_mode": "normal",

    # Orientation
    "hflip": False,
    "vflip": False,

    # Processing
    "noise_reduction": "auto",

    # Named profile shown in the UI
    "profile": "custom",
}

VIDEO_MODES = {
    "low": {"resolution": "320x240", "fps": 8, "quality": 60},
    "medium": {"resolution": "480x320", "fps": 10, "quality": 80},
    "high": {"resolution": "640x480", "fps": 12, "quality": 75},
    "hd": {"resolution": "1280x720", "fps": 15, "quality": 75},
}

RESOLUTIONS = [
    "320x240", "480x320", "640x480",
    "800x600", "1024x768", "1280x720",
    "1280x960", "1640x1232", "1920x1080",
]

FPS_OPTIONS = [5, 8, 10, 12, 15, 20, 24, 30]

PHOTO_PREVIEW_CONFIG = {
    "resolution": "640x480",
    "quality": 85,
}

PHOTO_SAVE_CONFIG = {
    "resolution": "3280x2464",
    "quality": 90,
}

PHOTO_CONFIG = PHOTO_PREVIEW_CONFIG.copy()

PHOTO_PREVIEW_RESOLUTIONS = [
    "640x480",
    "800x600",
    "1024x768",
    "1280x720",
    "1600x1200",
    "1920x1080",
    "2592x1944",
    "3280x2464",
]

PHOTO_SAVE_RESOLUTION = "3280x2464"

# ============================================================
# CAMERA STATE
# ============================================================

CAMERA_AVAILABLE = False
CAMERA_MODE = "video"
CAMERA_ACTIVE = False
CAMERA_CAPTURE_BUSY = False
# Sensor model reported by Picamera2 itself (e.g. "imx708", "ov5647"), not a
# user-editable label - lets two Pis with different camera modules show their
# real hardware in the Devices card instead of a generic placeholder.
CAMERA_MODEL = ""

picam2 = None
camera_started = False
camera_lock = threading.RLock()
last_frame = None
last_frame_time = 0

# Incremented whenever the camera is stopped or reconfigured.
# Old MJPEG generators exit when their generation becomes obsolete.
stream_generation = 0

# ============================================================
# JSON HELPERS
# ============================================================

def safe_read_json(filepath, default=None):
    if default is None:
        default = {}

    tmp_file = filepath + ".tmp"
    if os.path.exists(tmp_file):
        try:
            os.remove(tmp_file)
            print(f"[JSON] Removed stale tmp file: {tmp_file}", flush=True)
        except Exception as e:
            print(f"[JSON] Could not remove tmp file: {e}", flush=True)

    if not os.path.exists(filepath):
        return default

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[JSON] Read error: {e}, using default", flush=True)
        return default


def safe_write_json(filepath, data):
    tmp_file = filepath + ".tmp"
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, filepath)
        return True
    except Exception as e:
        print(f"[JSON] Write error: {e}", flush=True)
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
        except Exception:
            pass
        return False

# ============================================================
# SETTINGS
# ============================================================

def load_camera_settings():
    """Load camera settings from JSON file."""
    data = safe_read_json(CAMERA_CONFIG_FILE, {})
    if not data:
        return

    if "video" in data:
        VIDEO_CONFIG.update(data["video"])
    if "photo_preview" in data:
        PHOTO_CONFIG.update(data["photo_preview"])
    if "controls" in data and isinstance(data["controls"], dict):
        CAMERA_CONTROLS.update(data["controls"])

    print(
        f"[CAMERA] Loaded settings: preview={PHOTO_CONFIG['resolution']}@{PHOTO_CONFIG['quality']}%, "
        f"save={PHOTO_SAVE_CONFIG['resolution']}",
        flush=True,
    )


def save_camera_settings():
    data = {
        "video": VIDEO_CONFIG,
        "photo_preview": PHOTO_CONFIG,
        "photo_save": PHOTO_SAVE_CONFIG,
        "controls": CAMERA_CONTROLS,
    }
    safe_write_json(CAMERA_CONFIG_FILE, data)
    print("[CAMERA] Saved settings", flush=True)

# ============================================================
# BASIC CAMERA CONTROL
# ============================================================
if LIBCAMERA_AVAILABLE:
    AWB_MODE_MAP = {
        "auto": libcamera_controls.AwbModeEnum.Auto,
        "daylight": libcamera_controls.AwbModeEnum.Daylight,
        "cloudy": libcamera_controls.AwbModeEnum.Cloudy,
        "indoor": libcamera_controls.AwbModeEnum.Indoor,
        "tungsten": libcamera_controls.AwbModeEnum.Tungsten,
        "fluorescent": libcamera_controls.AwbModeEnum.Fluorescent,
        "incandescent": libcamera_controls.AwbModeEnum.Incandescent,
    }

    EXPOSURE_MODE_MAP = {
        "normal": libcamera_controls.AeExposureModeEnum.Normal,
        "short": libcamera_controls.AeExposureModeEnum.Short,
        "long": libcamera_controls.AeExposureModeEnum.Long,
    }

    NOISE_REDUCTION_MAP = {
        "off": libcamera_controls.draft.NoiseReductionModeEnum.Off,
        "minimal": libcamera_controls.draft.NoiseReductionModeEnum.Minimal,
        "fast": libcamera_controls.draft.NoiseReductionModeEnum.Fast,
        "high_quality": libcamera_controls.draft.NoiseReductionModeEnum.HighQuality,
        "zsl": libcamera_controls.draft.NoiseReductionModeEnum.ZSL,
        "auto": libcamera_controls.draft.NoiseReductionModeEnum.Fast,
    }
else:
    AWB_MODE_MAP = {}
    EXPOSURE_MODE_MAP = {}
    NOISE_REDUCTION_MAP = {}


def get_camera_transform():
    """Build orientation transform used by video and capture configurations."""
    if not LIBCAMERA_AVAILABLE:
        return None
    return Transform(
        hflip=bool(CAMERA_CONTROLS.get("hflip", False)),
        vflip=bool(CAMERA_CONTROLS.get("vflip", False)),
    )


def build_camera_controls():
    """Convert saved MeshCenter settings to Picamera2/libcamera controls."""
    if not LIBCAMERA_AVAILABLE:
        return {}
    awb_mode = str(CAMERA_CONTROLS.get("awb_mode", "auto")).lower()
    exposure_mode = str(
        CAMERA_CONTROLS.get("exposure_mode", "normal")
    ).lower()
    noise_reduction = str(
        CAMERA_CONTROLS.get("noise_reduction", "auto")
    ).lower()

    result = {
        "Brightness": float(CAMERA_CONTROLS.get("brightness", 0.0)),
        "Contrast": float(CAMERA_CONTROLS.get("contrast", 1.0)),
        "Saturation": float(CAMERA_CONTROLS.get("saturation", 1.0)),
        "Sharpness": float(CAMERA_CONTROLS.get("sharpness", 1.0)),
        "ExposureValue": float(
            CAMERA_CONTROLS.get("exposure_compensation", 0.0)
        ),
        "AeExposureMode": EXPOSURE_MODE_MAP.get(
            exposure_mode,
            libcamera_controls.AeExposureModeEnum.Normal,
        ),
        "NoiseReductionMode": NOISE_REDUCTION_MAP.get(
            noise_reduction,
            libcamera_controls.draft.NoiseReductionModeEnum.Fast,
        ),
    }

    if awb_mode == "manual":
        result["AwbEnable"] = False
        result["ColourGains"] = (
            float(CAMERA_CONTROLS.get("red_gain", 1.0)),
            float(CAMERA_CONTROLS.get("blue_gain", 1.0)),
        )
    else:
        result["AwbEnable"] = True
        result["AwbMode"] = AWB_MODE_MAP.get(
            awb_mode,
            libcamera_controls.AwbModeEnum.Auto,
        )

    return result


def filter_supported_controls(requested):
    """Drop any control the currently opened camera doesn't advertise.

    build_camera_controls() always includes the full CSI/ISP control set
    (Sharpness, NoiseReductionMode, etc.) unconditionally - correct for a
    real CSI sensor, but a USB webcam seen through libcamera's uvcvideo
    pipeline handler (see camera/csi_driver.py) typically advertises a
    much smaller set. Passing an unsupported control straight into
    Picamera2.create_preview_configuration()/create_still_configuration()
    raises immediately (confirmed live: "Control Sharpness is not
    advertised by libcamera") - this must run before controls reach
    either that or set_controls().
    """
    available = getattr(picam2, "camera_controls", {}) or {}
    supported = {
        name: value
        for name, value in requested.items()
        if name in available
    }
    skipped = sorted(set(requested) - set(supported))
    if skipped:
        print(
            f"[CAMERA] Unsupported controls skipped: {', '.join(skipped)}",
            flush=True,
        )
    return supported


def apply_camera_controls():
    """Apply supported controls to the currently configured camera."""
    if picam2 is None:
        return False

    requested = build_camera_controls()
    supported = filter_supported_controls(requested)

    if not supported:
        print("[CAMERA] No supported image controls to apply", flush=True)
        return False

    try:
        picam2.set_controls(supported)
        print(
            f"[CAMERA] Controls applied: {list(supported.keys())}",
            flush=True,
        )
        return True

    except Exception as e:
        print(f"[CAMERA] Control apply error: {e}", flush=True)
        return False

def fix_camera_colors(frame):
    """Convert BGR to RGB if needed."""
    if frame is not None and getattr(frame, "ndim", 0) == 3 and frame.shape[2] == 3:
        return frame[:, :, ::-1]
    return frame


def init_camera():
    """Initialize camera through Picamera2."""
    global CAMERA_AVAILABLE, CAMERA_MODEL, picam2

    print("[CAMERA] 🔍 Initializing...", flush=True)

    try:
        from picamera2 import Picamera2

        print("[CAMERA] ✅ Picamera2 imported", flush=True)
        picam2 = Picamera2()

        props = picam2.camera_properties
        if props:
            CAMERA_MODEL = str(props.get("Model") or "").strip()
            print(f"[CAMERA] ✅ Camera found: {CAMERA_MODEL or 'unknown model'}", flush=True)
            CAMERA_AVAILABLE = True
            return True

        print("[CAMERA] ❌ No camera properties", flush=True)
        CAMERA_AVAILABLE = False
        return False

    except Exception as e:
        print(f"[CAMERA] ❌ Init error: {e}", flush=True)
        CAMERA_AVAILABLE = False
        return False


def stop_camera():
    """Halt the capture pipeline but keep the Picamera2 object itself
    alive - used internally by switch_camera_mode() and the photo-retry
    paths, both of which need to reconfigure/retry on the same picam2
    instance right after calling this. Use close_camera() instead when
    the device should be fully released (e.g. camera powered off)."""
    global camera_started, CAMERA_ACTIVE
    global stream_generation, last_frame, last_frame_time

    with camera_lock:
        if camera_started and picam2 is not None:
            try:
                picam2.stop()
                print("[CAMERA] Stopped", flush=True)
            except Exception as e:
                print(f"[CAMERA] Stop error: {e}", flush=True)
        camera_started = False
        CAMERA_ACTIVE = False

        stream_generation += 1
        last_frame = None
        last_frame_time = 0

        return True


def close_camera():
    """Fully release the camera: stop the pipeline, close() the
    Picamera2 object, and drop the reference. Restores the behavior
    api_camera.py's close_camera_device() used to implement inline
    before the camera_manager cutover - CsiCameraDriver.stop() calls
    this (not stop_camera()) so powering the camera off actually frees
    the underlying resource instead of just halting capture."""
    global picam2, CAMERA_AVAILABLE

    stop_camera()

    with camera_lock:
        if picam2 is not None:
            try:
                picam2.close()
                print("[CAMERA] Closed", flush=True)
            except Exception as e:
                print(f"[CAMERA] Close error: {e}", flush=True)
            picam2 = None
        CAMERA_AVAILABLE = False


def switch_camera_mode(mode, resolution=None, fps=None):
    """Switch camera mode to video or photo."""
    global camera_started, CAMERA_MODE, CAMERA_ACTIVE

    if mode not in ["video", "photo"]:
        return False

    if not CAMERA_AVAILABLE or picam2 is None:
        return False

    with camera_lock:
        stop_camera()

        try:
            if mode == "video":
                w, h = map(
                    int,
                    (resolution or VIDEO_CONFIG["resolution"]).split("x")
                )
                fps_val = fps or VIDEO_CONFIG["fps"]

                initial_controls = build_camera_controls()
                initial_controls["FrameRate"] = fps_val
                initial_controls = filter_supported_controls(initial_controls)

                config = picam2.create_preview_configuration(
                    main={
                        "size": (w, h),
                        "format": "RGB888",
                    },
                    controls=initial_controls,
                    transform=get_camera_transform(),
                )

                picam2.configure(config)
                picam2.start()

                camera_started = True
                CAMERA_MODE = "video"
                CAMERA_ACTIVE = True

                print(
                    f"[CAMERA] Initial video controls configured: "
                    f"{list(initial_controls.keys())}",
                    flush=True,
                )

                print(
                    f"[CAMERA] Video mode: {w}x{h} @ {fps_val} fps",
                    flush=True,
                )

                return True

            # ------------------------------------------------
            # PHOTO MODE
            # ------------------------------------------------

            w, h = map(
                int,
                (resolution or PHOTO_CONFIG["resolution"]).split("x")
            )

            initial_controls = build_camera_controls()
            initial_controls = filter_supported_controls(initial_controls)

            config = picam2.create_still_configuration(
                main={
                    "size": (w, h),
                    "format": "RGB888",
                },
                controls=initial_controls,
                transform=get_camera_transform(),
            )

            picam2.configure(config)
            picam2.start()

            camera_started = True
            CAMERA_MODE = "photo"
            CAMERA_ACTIVE = True

            print(
                f"[CAMERA] Initial photo controls configured: "
                f"{list(initial_controls.keys())}",
                flush=True,
            )

            print(
                f"[CAMERA] Photo mode: {w}x{h}",
                flush=True,
            )

            return True

        except Exception as e:
            print(
                f"[CAMERA] Switch mode error: {e}",
                flush=True,
            )

            camera_started = False
            CAMERA_ACTIVE = False

            return False

def start_camera():
    """Start camera in video mode."""
    if not CAMERA_AVAILABLE:
        return False

    with camera_lock:
        if camera_started and CAMERA_MODE == "video":
            return True
        return switch_camera_mode("video")


def get_camera_frame():
    """Capture one camera frame."""
    global last_frame, last_frame_time, camera_started, CAMERA_CAPTURE_BUSY

    if CAMERA_CAPTURE_BUSY:
        time.sleep(0.05)
        return None

    if not camera_started:
        if not start_camera():
            return None

    with camera_lock:
        try:
            if CAMERA_CAPTURE_BUSY:
                return None

            if picam2 is None:
                return last_frame

            frame = picam2.capture_array()
            if frame is not None and frame.size > 0:
                frame = fix_camera_colors(frame)
                last_frame = frame
                last_frame_time = time.time()
                return frame

            return last_frame

        except Exception as e:
            print(f"[CAMERA] Frame error: {e}", flush=True)
            camera_started = False
            return last_frame

# ============================================================
# VIDEO STREAM
# ============================================================

def generate_mjpeg_stream():
    """Generate MJPEG frames for the current camera generation."""
    global stream_generation

    if not start_camera():
        print("[CAMERA] ❌ Cannot start camera", flush=True)
        return

    generation = stream_generation
    frame_interval = 1.0 / max(1, int(VIDEO_CONFIG["fps"]))
    last_send_time = 0
    quality = int(VIDEO_CONFIG["quality"])

    print(
        f"[CAMERA] 🎥 MJPEG stream started: "
        f"{VIDEO_CONFIG['resolution']} @ {VIDEO_CONFIG['fps']} fps "
        f"(generation {generation})",
        flush=True,
    )

    while generation == stream_generation:
        try:
            current_time = time.time()

            if current_time - last_send_time < frame_interval:
                time.sleep(0.01)
                continue

            frame = get_camera_frame()

            if generation != stream_generation:
                break

            if frame is None:
                time.sleep(0.05)
                continue

            img = Image.fromarray(frame)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            jpeg_data = buf.getvalue()

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache, no-store, must-revalidate\r\n"
                b"Pragma: no-cache\r\n"
                b"Expires: 0\r\n\r\n"
                + jpeg_data
                + b"\r\n"
            )

            last_send_time = current_time

        except GeneratorExit:
            break

        except Exception as e:
            if generation != stream_generation:
                break

            print(f"[CAMERA] Stream error: {e}", flush=True)
            time.sleep(0.25)

    print(
        f"[CAMERA] MJPEG stream stopped "
        f"(generation {generation})",
        flush=True,
    )

# ============================================================
# SCREENSHOTS / GALLERY
# ============================================================

def get_screenshot_day_dir(dt=None):
    if dt is None:
        dt = datetime.now()

    day_dir = os.path.join(
        SCREENSHOTS_DIR,
        dt.strftime("%Y"),
        dt.strftime("%m"),
        dt.strftime("%d")
    )
    os.makedirs(day_dir, exist_ok=True)
    return day_dir


def make_screenshot_filename(dt=None, prefix="MC"):
    if dt is None:
        dt = datetime.now()
    return f"{prefix}_{dt.strftime('%Y-%m-%d_%H-%M-%S')}.jpg"


def iter_screenshot_files():
    items = []

    if not os.path.exists(SCREENSHOTS_DIR):
        return items

    for root, dirs, files in os.walk(SCREENSHOTS_DIR):
        for filename in files:
            if not filename.lower().endswith(".jpg"):
                continue

            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, SCREENSHOTS_DIR).replace("\\", "/")

            try:
                stat = os.stat(full_path)
                items.append((full_path, rel_path, stat))
            except OSError:
                pass

    return items


def safe_screenshot_path(filename):
    filename = filename.replace("\\", "/")
    base = os.path.abspath(SCREENSHOTS_DIR)
    path = os.path.abspath(os.path.join(base, filename))

    if not path.startswith(base + os.sep):
        return None

    return path


def cleanup_old_screenshots(max_mb=500, keep_days=30):
    files = iter_screenshot_files()
    if not files:
        return

    now = time.time()
    keep_seconds = keep_days * 86400
    max_bytes = max_mb * 1024 * 1024

    for full_path, rel_path, stat in files:
        if now - stat.st_mtime > keep_seconds:
            try:
                os.remove(full_path)
            except OSError:
                pass

    files = iter_screenshot_files()
    total_size = sum(stat.st_size for _, _, stat in files)

    if total_size <= max_bytes:
        return

    files.sort(key=lambda x: x[2].st_mtime)

    for full_path, rel_path, stat in files:
        if total_size <= max_bytes:
            break

        try:
            os.remove(full_path)
            total_size -= stat.st_size
        except OSError:
            pass


def capture_screenshot():
    """Create screenshot from current video frame."""
    if not camera_started:
        if not start_camera():
            return {"success": False, "error": "Camera not ready"}

    try:
        from PIL import Image

        frame = get_camera_frame()
        if frame is None:
            return {"success": False, "error": "Failed to capture frame"}

        img = Image.fromarray(frame)

        dt = datetime.now()
        day_dir = get_screenshot_day_dir(dt)
        filename = make_screenshot_filename(dt, prefix="MC")
        filepath = os.path.join(day_dir, filename)

        quality = VIDEO_CONFIG.get("quality", 90)
        img.save(filepath, "JPEG", quality=quality)

        rel_path = os.path.relpath(filepath, SCREENSHOTS_DIR).replace("\\", "/")

        cleanup_old_screenshots(max_mb=500, keep_days=30)

        return {
            "success": True,
            "filename": rel_path,
            "display_name": filename,
            "filepath": filepath,
            "size": os.path.getsize(filepath),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


def list_screenshots():
    try:
        files = []

        for full_path, rel_path, stat in iter_screenshot_files():
            files.append({
                "filename": rel_path,
                "display_name": os.path.basename(rel_path),
                "size": stat.st_size,
                "modified_ts": stat.st_mtime,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "date": time.strftime("%Y-%m-%d", time.localtime(stat.st_mtime)),
                "url": f"/api/camera/screenshot/{rel_path}",
            })

        files.sort(key=lambda x: x["modified_ts"], reverse=True)

        return {
            "screenshots": files,
            "storage": get_gallery_storage_info()
        }, 200

    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def delete_screenshot(filename):
    filepath = safe_screenshot_path(filename)

    if filepath is None or not os.path.isfile(filepath):
        return {"ok": False, "error": "File not found"}, 404

    os.remove(filepath)
    return {"ok": True, "message": f"Deleted {filename}"}, 200


def delete_all_screenshots():
    count = 0

    for full_path, rel_path, stat in iter_screenshot_files():
        try:
            os.remove(full_path)
            count += 1
        except OSError as e:
            print(f"[GALLERY] Could not delete {rel_path}: {e}", flush=True)

    return {"ok": True, "deleted_count": count}, 200

def get_gallery_storage_info():
    try:
        files = iter_screenshot_files()
        total_size = sum(stat.st_size for _, _, stat in files)

        usage = os.statvfs(SCREENSHOTS_DIR)
        free_bytes = usage.f_bavail * usage.f_frsize
        total_bytes = usage.f_blocks * usage.f_frsize

        return {
            "images": len(files),
            "used_bytes": total_size,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "used_mb": round(total_size / 1024 / 1024, 1),
            "free_gb": round(free_bytes / 1024 / 1024 / 1024, 2),
            "total_gb": round(total_bytes / 1024 / 1024 / 1024, 2),
        }
    except Exception as e:
        return {
            "images": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "total_bytes": 0,
            "used_mb": 0,
            "free_gb": 0,
            "total_gb": 0,
            "error": str(e),
        }

# ============================================================
# API-LIKE HELPERS FOR SERVER ROUTES
# ============================================================

def get_camera_status():
    return {
        "ok": CAMERA_AVAILABLE,
        "started": camera_started,
        "model": CAMERA_MODEL,
        "mode": CAMERA_MODE,
        "resolution": VIDEO_CONFIG["resolution"],
        "fps": VIDEO_CONFIG["fps"],
        "quality": VIDEO_CONFIG["quality"],
        "available_resolutions": RESOLUTIONS,
        "available_fps": FPS_OPTIONS,
        "video_modes": VIDEO_MODES,
        "controls": CAMERA_CONTROLS.copy(),
    }


def api_switch_mode(data):
    mode = data.get("mode", "video")
    if mode not in ["video", "photo"]:
        return {"ok": False, "error": "Invalid mode"}, 400

    ok = switch_camera_mode(mode)
    if not ok:
        return {"ok": False, "error": "Failed to switch camera mode"}, 500

    resolution = VIDEO_CONFIG["resolution"] if mode == "video" else PHOTO_CONFIG["resolution"]
    return {"ok": True, "mode": mode, "resolution": resolution}, 200


def get_camera_settings():
    return {
        "ok": True,
        "config": VIDEO_CONFIG.copy(),
        "controls": CAMERA_CONTROLS.copy(),
        "available_resolutions": RESOLUTIONS,
        "available_fps": FPS_OPTIONS,
        "video_modes": VIDEO_MODES,
    }


def update_camera_settings(data):
    video_changes = {}
    control_changes = {}

    # --------------------------------------------------------
    # VIDEO SETTINGS
    # --------------------------------------------------------

    if "resolution" in data:
        res = str(data["resolution"])
        if res not in RESOLUTIONS:
            return {
                "ok": False,
                "error": f"Invalid resolution. Available: {RESOLUTIONS}"
            }, 400
        video_changes["resolution"] = res

    if "fps" in data:
        try:
            fps = int(data["fps"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "FPS must be an integer"}, 400

        if fps not in FPS_OPTIONS:
            return {
                "ok": False,
                "error": f"Invalid FPS. Available: {FPS_OPTIONS}"
            }, 400

        video_changes["fps"] = fps

    if "quality" in data:
        try:
            quality = int(data["quality"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "Quality must be an integer"}, 400

        if not 40 <= quality <= 90:
            return {
                "ok": False,
                "error": "Quality must be between 40 and 90"
            }, 400

        video_changes["quality"] = quality

    # --------------------------------------------------------
    # CAMERA CONTROLS
    # --------------------------------------------------------

    controls_data = data.get("controls")

    if controls_data is not None:
        if not isinstance(controls_data, dict):
            return {
                "ok": False,
                "error": "controls must be an object"
            }, 400

        numeric_limits = {
            "brightness": (-1.0, 1.0),
            "contrast": (0.0, 32.0),
            "saturation": (0.0, 32.0),
            "sharpness": (0.0, 16.0),
            "exposure_compensation": (-8.0, 8.0),
            "red_gain": (0.0, 32.0),
            "blue_gain": (0.0, 32.0),
        }

        for name, limits in numeric_limits.items():
            if name not in controls_data:
                continue

            try:
                value = float(controls_data[name])
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": f"{name} must be numeric"
                }, 400

            minimum, maximum = limits
            if not minimum <= value <= maximum:
                return {
                    "ok": False,
                    "error": (
                        f"{name} must be between "
                        f"{minimum} and {maximum}"
                    )
                }, 400

            control_changes[name] = value

        if "awb_mode" in controls_data:
            awb_mode = str(controls_data["awb_mode"]).lower()

            allowed_awb_modes = {
                "auto",
                "daylight",
                "cloudy",
                "indoor",
                "tungsten",
                "fluorescent",
                "incandescent",
                "manual",
            }

            if awb_mode not in allowed_awb_modes:
                return {
                    "ok": False,
                    "error": (
                        "Invalid AWB mode. Available: "
                        + ", ".join(sorted(allowed_awb_modes))
                    )
                }, 400

            control_changes["awb_mode"] = awb_mode

        if "exposure_mode" in controls_data:
            exposure_mode = str(
                controls_data["exposure_mode"]
            ).lower()

            allowed_exposure_modes = {
                "normal",
                "short",
                "long",
            }

            if exposure_mode not in allowed_exposure_modes:
                return {
                    "ok": False,
                    "error": (
                        "Invalid exposure mode. Available: "
                        + ", ".join(sorted(allowed_exposure_modes))
                    )
                }, 400

            control_changes["exposure_mode"] = exposure_mode

        if "noise_reduction" in controls_data:
            noise_reduction = str(
                controls_data["noise_reduction"]
            ).lower()

            allowed_noise_modes = {
                "auto",
                "off",
                "minimal",
                "fast",
                "high_quality",
                "zsl",
            }

            if noise_reduction not in allowed_noise_modes:
                return {
                    "ok": False,
                    "error": (
                        "Invalid noise reduction mode. Available: "
                        + ", ".join(sorted(allowed_noise_modes))
                    )
                }, 400

            control_changes["noise_reduction"] = noise_reduction

        for name in ("hflip", "vflip"):
            if name not in controls_data:
                continue

            value = controls_data[name]

            if not isinstance(value, bool):
                return {
                    "ok": False,
                    "error": f"{name} must be true or false"
                }, 400

            control_changes[name] = value

    # --------------------------------------------------------
    # APPLY SETTINGS
    # --------------------------------------------------------

    if not video_changes and not control_changes:
        return {
            "ok": True,
            "config": VIDEO_CONFIG.copy(),
            "controls": CAMERA_CONTROLS.copy(),
            "video_changes": {},
            "control_changes": {},
        }, 200

    # Resolution, FPS and ISP controls require a Picamera2 pipeline
    # restart. JPEG quality is consumed by a new MJPEG generator and
    # must not restart the camera hardware by itself.
    pipeline_video_changes = {
        name: value
        for name, value in video_changes.items()
        if name in {"resolution", "fps"}
    }
    video_restart_required = bool(pipeline_video_changes or control_changes)

    with camera_lock:
        was_started = camera_started
        previous_mode = CAMERA_MODE

        VIDEO_CONFIG.update(video_changes)
        CAMERA_CONTROLS.update(control_changes)

        if control_changes:
            CAMERA_CONTROLS["profile"] = "custom"

        save_camera_settings()

        if was_started and video_restart_required:
            stop_camera()

            if previous_mode == "photo":
                switch_camera_mode(
                    "photo",
                    resolution=PHOTO_CONFIG["resolution"]
                )
            else:
                switch_camera_mode(
                    "video",
                    resolution=VIDEO_CONFIG["resolution"],
                    fps=VIDEO_CONFIG["fps"]
                )

    if video_changes:
        print(
            f"[CAMERA] Video settings updated: {video_changes}",
            flush=True,
        )

    if control_changes:
        print(
            f"[CAMERA] Image controls updated: {control_changes}",
            flush=True,
        )

    return {
        "ok": True,
        "config": VIDEO_CONFIG.copy(),
        "controls": CAMERA_CONTROLS.copy(),
        "video_changes": video_changes,
        "control_changes": control_changes,
        "restarted": bool(was_started and video_restart_required),
    }, 200


def set_video_mode(mode):
    if mode not in VIDEO_MODES:
        return {"ok": False, "error": f"Invalid mode. Available: {list(VIDEO_MODES.keys())}"}, 400

    config = VIDEO_MODES[mode]
    with camera_lock:
        stop_camera()
        VIDEO_CONFIG.update(config)
        save_camera_settings()
        start_camera()

    print(f"[CAMERA] ✅ Switched to mode: {mode} ({config['resolution']} @ {config['fps']} fps)", flush=True)
    return {"ok": True, "mode": mode, "config": VIDEO_CONFIG}, 200

# ============================================================
# PHOTO
# ============================================================

def get_photo_settings():
    return {
        "ok": True,
        "config": PHOTO_CONFIG.copy(),
        "save_config": PHOTO_SAVE_CONFIG.copy(),
        "controls": CAMERA_CONTROLS.copy(),
        "available_resolutions": PHOTO_PREVIEW_RESOLUTIONS,
        "save_resolution": PHOTO_SAVE_RESOLUTION,
    }


def update_photo_settings(data):
    changes = {}

    if "resolution" in data:
        res = data["resolution"]
        if res not in PHOTO_PREVIEW_RESOLUTIONS:
            return {"ok": False, "error": f"Invalid resolution. Available: {PHOTO_PREVIEW_RESOLUTIONS}"}, 400
        changes["resolution"] = res

    if "quality" in data:
        quality = int(data["quality"])
        if not 70 <= quality <= 100:
            return {"ok": False, "error": "Quality must be between 70 and 100"}, 400
        changes["quality"] = quality

    if changes:
        PHOTO_CONFIG.update(changes)
        save_camera_settings()
        print(f"[PHOTO] ✅ Settings updated: {changes}", flush=True)

    return {"ok": True, "config": PHOTO_CONFIG, "changes": changes}, 200


def _capture_still(resolution, quality, log_prefix="[PHOTO]"):
    from PIL import Image

    w, h = map(int, resolution.split("x"))
    print(f"{log_prefix} Capturing: {w}x{h}, quality={quality}%", flush=True)

    ok = switch_camera_mode("photo", resolution=resolution)
    if not ok:
        raise RuntimeError("Failed to switch camera to photo mode")

    time.sleep(1.5)

    last_size = None

    for attempt in range(8):
        frame = picam2.capture_array()
        img = Image.fromarray(fix_camera_colors(frame))
        real_w, real_h = img.size
        last_size = (real_w, real_h)

        print(f"{log_prefix} Attempt {attempt + 1}: {real_w}x{real_h}", flush=True)

        if real_w == w and real_h == h:
            return img, real_w, real_h

        time.sleep(0.25)

    raise RuntimeError(f"Wrong capture size. Expected {w}x{h}, got {last_size[0]}x{last_size[1]}")


def capture_photo_preview():
    if not CAMERA_AVAILABLE:
        return {"ok": False, "error": "Camera not available"}, 503

    try:
        quality = PHOTO_CONFIG.get("quality", 85)
        img, w, h = _capture_still(PHOTO_CONFIG["resolution"], quality, "[PHOTO PREVIEW]")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        jpeg_data = base64.b64encode(buf.getvalue()).decode("utf-8")

        return {
            "ok": True,
            "image_data": jpeg_data,
            "width": w,
            "height": h,
            "preview_resolution": PHOTO_CONFIG["resolution"],
            "quality": quality,
            "mode": "photo",
        }, 200

    except Exception as e:
        print(f"[PHOTO] ❌ Capture error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            stop_camera()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}, 500


