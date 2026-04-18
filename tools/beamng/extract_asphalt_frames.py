"""Estrae frame GoPro per riferimento asfalto.

GX010576.MP4: rettilineo da t=34s
GX010577.MP4: continuazione
"""
from pathlib import Path
import cv2
import sys

SRC_DIR = Path(r"C:\Users\Matto\Desktop\mcrn")
OUT_DIR = Path(r"C:\Users\Matto\Desktop\Macerozz\tools\beamng\asphalt_refs")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# (video_path, timestamp_sec, label)
TARGETS = [
    (SRC_DIR / "GX010576.MP4", 36.0,  "v1_straight_start"),
    (SRC_DIR / "GX010576.MP4", 45.0,  "v1_straight_mid"),
    (SRC_DIR / "GX010576.MP4", 60.0,  "v1_straight_far"),
    (SRC_DIR / "GX010576.MP4", 90.0,  "v1_late_90s"),
    (SRC_DIR / "GX010576.MP4", 120.0, "v1_late_120s"),
    (SRC_DIR / "GX010576.MP4", 180.0, "v1_late_180s"),
    (SRC_DIR / "GX010577.MP4", 10.0,  "v2_early"),
    (SRC_DIR / "GX010577.MP4", 40.0,  "v2_mid"),
    (SRC_DIR / "GX010577.MP4", 80.0,  "v2_late"),
    (SRC_DIR / "GX010577.MP4", 140.0, "v2_far"),
]

def extract(video: Path, t_sec: float, label: str):
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"FAIL open {video}")
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    duration = total_frames / fps if fps else 0
    if t_sec > duration:
        print(f"  skip {label}: t={t_sec} > dur={duration:.1f}")
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_POS_MSEC, t_sec * 1000.0)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        print(f"  read fail {label}")
        return None
    # resize a 1280x720 per contenimento (mantiene aspect)
    h, w = frame.shape[:2]
    scale = 1280 / w
    frame = cv2.resize(frame, (1280, int(h * scale)))
    out_path = OUT_DIR / f"{label}.jpg"
    cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
    size_kb = out_path.stat().st_size / 1024
    print(f"  {label}: {out_path.name} ({size_kb:.0f} KB, {frame.shape[1]}x{frame.shape[0]}, fps={fps:.1f})")
    return out_path

if __name__ == "__main__":
    print(f"Output: {OUT_DIR}")
    for video, t, label in TARGETS:
        if not video.exists():
            print(f"MISSING: {video}")
            continue
        extract(video, t, label)
    print("\nDone.")
