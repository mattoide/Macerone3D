"""
Genera texture PBR procedurali per la mod BeamNG.drive:
  art/road/asphalt_base.png        (512x512 RGB)
  art/road/asphalt_normal.png      (512x512 RGB - tangent space)
  art/road/asphalt_roughness.png   (512x512 grayscale)

Semplici ma "credibili": base color = grigio scuro con noise cluster (ghiaia),
roughness alta con variazione, normal derivato dalla luminanza del base.

Usa solo PIL + numpy (gia' dipendenze del progetto).
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image

OUT_DIR = (Path(__file__).resolve().parents[2]
           / "output" / "beamng" / "mod" / "levels" / "macerone" / "art"
           / "road")

SIZE = 512
BASE_COLOR = np.array([55, 57, 60], dtype=np.float32)  # asfalto scuro
BASE_VARIATION = 30.0
GRAVEL_DENSITY = 0.12
GRAVEL_RADIUS_RANGE = (1.5, 3.5)
SEED = 42


def value_noise(size: int, octaves: int = 5, persistence: float = 0.5,
                 seed: int = 0) -> np.ndarray:
    """Value noise semplice, output in [0,1]. Seamless sui bordi (tile)."""
    rng = np.random.default_rng(seed)
    total = np.zeros((size, size), dtype=np.float32)
    amplitude = 1.0
    max_amp = 0.0
    for o in range(octaves):
        freq = 2 ** o
        # griglia a bassa risoluzione
        grid = rng.random((freq + 1, freq + 1), dtype=np.float32)
        # fai combaciare i bordi per tiling
        grid[-1, :] = grid[0, :]
        grid[:, -1] = grid[:, 0]
        # upsample bilineare
        img = Image.fromarray(grid, mode="F").resize((size, size),
                                                       Image.BILINEAR)
        total += np.array(img, dtype=np.float32) * amplitude
        max_amp += amplitude
        amplitude *= persistence
    total /= max_amp
    return total


def asphalt_base(size: int, seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = value_noise(size, octaves=6, persistence=0.55, seed=seed)
    # Varia luminosita' del grigio
    lum = BASE_VARIATION * (noise - 0.5)
    rgb = np.clip(BASE_COLOR[None, None, :] + lum[:, :, None],
                   0, 255).astype(np.float32)
    # Aggiungi "ghiaia" come cluster chiari/scuri
    n_gravel = int(GRAVEL_DENSITY * size * size / 10)
    ys = rng.integers(0, size, n_gravel)
    xs = rng.integers(0, size, n_gravel)
    radii = rng.uniform(*GRAVEL_RADIUS_RANGE, n_gravel)
    brightness = rng.uniform(-20, 45, n_gravel)
    yy, xx = np.mgrid[0:size, 0:size]
    for y, x, r, b in zip(ys, xs, radii, brightness):
        # distanza toroidale per seamless
        dy = np.minimum(np.abs(yy - y), size - np.abs(yy - y))
        dx = np.minimum(np.abs(xx - x), size - np.abs(xx - x))
        d2 = dy * dy + dx * dx
        mask = d2 < r * r
        rgb[mask] = np.clip(rgb[mask] + b, 0, 255)
    # Crepa orizzontale subtle
    for _ in range(3):
        y0 = rng.integers(0, size)
        thickness = rng.integers(1, 2)
        wobble = (np.sin(np.linspace(0, 2 * np.pi * rng.integers(1, 4), size))
                   * rng.uniform(2, 6)).astype(int)
        for x in range(size):
            ys_line = (y0 + wobble[x]) % size
            for dy in range(-thickness, thickness + 1):
                rgb[(ys_line + dy) % size, x] = np.clip(
                    rgb[(ys_line + dy) % size, x] - 25, 0, 255)
    return rgb.astype(np.uint8)


def normal_from_height(height: np.ndarray, strength: float = 2.0) -> np.ndarray:
    """Sobel-style normal map in tangent space. Output uint8 RGB."""
    h = height.astype(np.float32) / 255.0
    # gradienti (seamless via np.roll)
    gx = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * 0.5 * strength
    gy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * 0.5 * strength
    nz = np.ones_like(h)
    length = np.sqrt(gx * gx + gy * gy + nz * nz)
    nx = -gx / length
    ny = -gy / length
    nz /= length
    # rimappa a [0,255]
    r = ((nx + 1.0) * 127.5).astype(np.uint8)
    g = ((ny + 1.0) * 127.5).astype(np.uint8)
    b = ((nz + 1.0) * 127.5).astype(np.uint8)
    return np.stack([r, g, b], axis=-1)


def asphalt_roughness(size: int, seed: int = SEED) -> np.ndarray:
    noise = value_noise(size, octaves=4, persistence=0.6, seed=seed + 7)
    rough = 0.75 + 0.22 * (noise - 0.5) * 2  # ~0.53 - 0.97
    return (np.clip(rough, 0, 1) * 255).astype(np.uint8)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Genero texture asfalto {SIZE}x{SIZE} in {OUT_DIR}")
    base = asphalt_base(SIZE)
    Image.fromarray(base, mode="RGB").save(OUT_DIR / "asphalt_base.png")
    print(f"  asphalt_base.png")

    # luminanza del base come heightmap per il normal
    lum = (0.299 * base[:, :, 0] + 0.587 * base[:, :, 1]
           + 0.114 * base[:, :, 2]).astype(np.uint8)
    normal = normal_from_height(lum, strength=3.5)
    Image.fromarray(normal, mode="RGB").save(OUT_DIR / "asphalt_normal.png")
    print(f"  asphalt_normal.png")

    rough = asphalt_roughness(SIZE)
    Image.fromarray(rough, mode="L").save(OUT_DIR / "asphalt_roughness.png")
    print(f"  asphalt_roughness.png")

    print("OK")


if __name__ == "__main__":
    main()
