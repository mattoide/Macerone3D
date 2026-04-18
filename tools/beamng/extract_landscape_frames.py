"""Estrae frame GoPro densi lungo il tragitto per analisi landscape.

Logica: campiono ogni 6 secondi da sec 34 di GX010576.MP4 (inizio
rettilineo SS17 reale) fino alla fine, poi continuazione su GX010577.MP4.
Ogni frame viene salvato con il timestamp nel nome, cosi' posso poi
correlare posizione lungo tragitto -> densita' alberi / presenza edifici.

Output: tools/beamng/landscape_refs/t<NNN>s_v<video>.jpg
"""
from pathlib import Path
import cv2

SRC_DIR = Path(r"C:\Users\Matto\Desktop\mcrn")
OUT_DIR = Path(r"C:\Users\Matto\Desktop\Macerozz\tools\beamng\landscape_refs")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# (video_path, start_sec, step_sec, max_sec)
SESSIONS = [
    (SRC_DIR / "GX010576.MP4", 34.0, 6.0, 9999),
    (SRC_DIR / "GX010577.MP4",  0.0, 6.0, 9999),
]


def extract_session(video: Path, start: float, step: float, max_s: float,
                     tag: str):
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"FAIL open {video}")
        return 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    dur = total / fps if fps else 0
    n_written = 0
    t = start
    while t < min(dur, max_s):
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        h, w = frame.shape[:2]
        scale = 960 / w
        frame = cv2.resize(frame, (960, int(h * scale)))
        t_int = int(round(t))
        out = OUT_DIR / f"{tag}_t{t_int:04d}s.jpg"
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        n_written += 1
        t += step
    cap.release()
    print(f"  {video.name}: {n_written} frame da {start}s step {step}s (dur {dur:.1f}s)")
    return n_written


if __name__ == "__main__":
    print(f"Output: {OUT_DIR}")
    total = 0
    total += extract_session(SRC_DIR / "GX010576.MP4", 34.0, 6.0, 9999, "v1")
    total += extract_session(SRC_DIR / "GX010577.MP4",  0.0, 6.0, 9999, "v2")
    print(f"\nTotale: {total} frame")
