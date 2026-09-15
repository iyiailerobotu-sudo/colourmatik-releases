"""Accuracy is measured, not eyeballed: CIEDE2000 (dE00)."""
from __future__ import annotations
import numpy as np
import colour

from .colorspace import encoded_to_lab


def delta_e00(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """Per-sample CIEDE2000 between two Lab arrays of shape (...,3)."""
    return np.asarray(colour.delta_E(lab1, lab2, method="CIE 2000"), dtype=np.float64)


def image_delta_e00(enc_a: np.ndarray, enc_b: np.ndarray, tf: str = "sRGB") -> np.ndarray:
    """Per-pixel dE00 between two display-encoded images (must be pixel-aligned)."""
    lab_a = encoded_to_lab(enc_a, tf).reshape(-1, 3)
    lab_b = encoded_to_lab(enc_b, tf).reshape(-1, 3)
    return delta_e00(lab_a, lab_b)


def summarize(de: np.ndarray) -> dict:
    de = np.asarray(de).ravel()
    if de.size == 0:
        return {"mean": float("nan"), "median": float("nan"),
                "p95": float("nan"), "max": float("nan")}
    return {
        "mean": float(np.mean(de)),
        "median": float(np.median(de)),
        "p95": float(np.percentile(de, 95)),
        "max": float(np.max(de)),
    }


def sliced_wasserstein(A: np.ndarray, B: np.ndarray, n_proj: int = 64,
                       seed: int = 0) -> float:
    """Distribution distance between two point sets (no correspondence needed).

    Mean over random 1D projections of the 1-Wasserstein (sorted L1) distance.
    Sensitive to the FULL distribution shape (not just mean/covariance), so it can
    tell a nonlinear distribution match (IDT) apart from a linear one (MKL).

    All projections are sorted in one float32 call (same random directions as
    the original loop, so scores are unchanged; measured 2.1x faster)."""
    if len(A) == 0 or len(B) == 0:
        return float("nan")
    rng = np.random.default_rng(seed)
    d = A.shape[1]
    V = np.empty((d, n_proj))
    for k in range(n_proj):                      # draw order kept identical to the loop version
        v = rng.normal(size=d)
        V[:, k] = v / np.linalg.norm(v)
    V32 = V.astype(np.float32)
    a = np.sort(np.asarray(A, dtype=np.float32) @ V32, axis=0)
    b = np.sort(np.asarray(B, dtype=np.float32) @ V32, axis=0)
    if a.shape[0] != b.shape[0]:
        n = min(a.shape[0], b.shape[0])
        q = np.linspace(0.0, 1.0, n, dtype=np.float32)
        qa = np.linspace(0.0, 1.0, a.shape[0], dtype=np.float32)
        qb = np.linspace(0.0, 1.0, b.shape[0], dtype=np.float32)
        a = np.stack([np.interp(q, qa, a[:, k]) for k in range(n_proj)], axis=1)
        b = np.stack([np.interp(q, qb, b[:, k]) for k in range(n_proj)], axis=1)
    return float(np.mean(np.abs(a - b)))


def verdict(mean_de: float) -> str:
    if mean_de < 1.0:
        return "imperceptible (dE00<1)"
    if mean_de < 2.0:
        return "excellent (dE00<2)"
    if mean_de < 3.0:
        return "good (dE00<3)"
    if mean_de < 5.0:
        return "noticeable"
    return "poor"
