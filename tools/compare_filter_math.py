#!/usr/bin/env python3
"""
Compare Python filter math with the Rust prototype to ensure parity.

Usage:
    python3 tools/compare_filter_math.py
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Tuple

import numpy as np

try:
    from motion_tracker_rs import (
        es_ekf_predict,
        kalman_update,
        predict_position,
        propagate_covariance,
    )
except ImportError as exc:  # pragma: no cover - helpful message for first-time setup
    raise SystemExit(
        "motion_tracker_rs module not installed. "
        "Run `cd motion_tracker_rs && ~/.local/bin/maturin build` "
        "followed by `pip install --user target/wheels/...whl`."
    ) from exc


@dataclass
class ParityResult:
    position_max_abs_error: float
    covariance_max_abs_error: float
    es_state_max_abs_error: float
    es_covariance_max_abs_error: float


def python_predict_position(position: np.ndarray, velocity: np.ndarray, dt: float) -> np.ndarray:
    return position + velocity * dt


def python_propagate_covariance(F: np.ndarray, P: np.ndarray, Q: np.ndarray) -> np.ndarray:
    return F @ P @ F.T + Q


def python_kalman_update(
    state: np.ndarray,
    covariance: np.ndarray,
    measurement_matrix: np.ndarray,
    residual: np.ndarray,
    measurement_noise: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    H = measurement_matrix
    R = measurement_noise
    z = residual.reshape(-1, 1)
    x = state.reshape(-1, 1)

    S = H @ covariance @ H.T + R
    try:
        S_inv = np.linalg.inv(S)
    except np.linalg.LinAlgError:
        S = S + 1e-9 * np.eye(S.shape[0])
        S_inv = np.linalg.inv(S)

    K = covariance @ H.T @ S_inv
    dx = K @ z
    x = x + dx

    I_KH = np.eye(covariance.shape[0]) - K @ H
    P = I_KH @ covariance @ I_KH.T + K @ R @ K.T

    return x.flatten(), P


def es_ekf_state_transition_jacobian(dt: float) -> np.ndarray:
    dt2 = dt * dt
    return np.array([
        [1.0, 0.0, dt, 0.0, 0.5 * dt2, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, dt, 0.0, 0.5 * dt2, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0, dt, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 1.0, 0.0, dt, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, dt],
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
    ])


def python_es_ekf_predict(
    state: np.ndarray,
    covariance: np.ndarray,
    process_noise: np.ndarray,
    dt: float,
) -> Tuple[np.ndarray, np.ndarray]:
    next_state = state.copy()

    vx, vy = next_state[2], next_state[3]
    ax, ay = next_state[4], next_state[5]
    heading = next_state[6]
    heading_rate = next_state[7]

    vel_mag = math.hypot(vx, vy)
    vx_pred = vel_mag * math.cos(heading)
    vy_pred = vel_mag * math.sin(heading)

    next_state[0] += vx_pred * dt + 0.5 * ax * dt * dt
    next_state[1] += vy_pred * dt + 0.5 * ay * dt * dt
    next_state[2] += ax * dt
    next_state[3] += ay * dt
    next_state[6] += heading_rate * dt

    F = es_ekf_state_transition_jacobian(dt)
    next_cov = F @ covariance @ F.T + process_noise

    return next_state, next_cov


def generate_inputs(dim: int = 3) -> Tuple[np.ndarray, np.ndarray, float]:
    rng = random.Random(42)
    position = np.array([rng.uniform(-10, 10) for _ in range(dim)], dtype=np.float64)
    velocity = np.array([rng.uniform(-5, 5) for _ in range(dim)], dtype=np.float64)
    dt = rng.uniform(0.01, 0.2)
    return position, velocity, dt


def run_parity_tests(trials: int = 64) -> ParityResult:
    max_position_error = 0.0
    max_cov_error = 0.0
    max_es_state_error = 0.0
    max_es_cov_error = 0.0

    rng = np.random.default_rng(seed=1234)

    for _ in range(trials):
        position, velocity, dt = generate_inputs()
        py_position = python_predict_position(position, velocity, dt)
        rs_position = predict_position(position, velocity, dt)
        max_position_error = max(max_position_error, float(np.max(np.abs(py_position - rs_position))))

        F = rng.normal(size=(3, 3))
        P = rng.normal(size=(3, 3))
        P = P @ P.T + np.eye(3) * 1e-3  # make SPD-ish
        Q = np.eye(3) * rng.uniform(1e-3, 1e-2)

        py_cov = python_propagate_covariance(F, P, Q)
        rs_cov = propagate_covariance(F, P, Q)
        max_cov_error = max(max_cov_error, float(np.max(np.abs(py_cov - rs_cov))))

        state = rng.normal(size=8)
        covariance = rng.normal(size=(8, 8))
        covariance = covariance @ covariance.T + np.eye(8) * 1e-3
        measurement_matrix = rng.normal(size=(2, 8))
        residual = rng.normal(size=2)
        measurement_noise = np.eye(2) * rng.uniform(1e-3, 1e-1)

        py_state_upd, py_cov_upd = python_kalman_update(
            state, covariance, measurement_matrix, residual, measurement_noise
        )
        rs_state_upd, rs_cov_upd = kalman_update(
            state, covariance, measurement_matrix, residual, measurement_noise
        )
        max_cov_error = max(
            max_cov_error,
            float(np.max(np.abs(py_cov_upd - rs_cov_upd))),
        )
        max_position_error = max(
            max_position_error,
            float(np.max(np.abs(py_state_upd - rs_state_upd))),
        )

        state = rng.normal(size=8)
        covariance = rng.normal(size=(8, 8))
        covariance = covariance @ covariance.T + np.eye(8) * 1e-3
        process_noise = np.eye(8) * rng.uniform(1e-4, 1e-2)
        dt = rng.uniform(0.01, 0.1)

        py_state, py_cov = python_es_ekf_predict(state, covariance, process_noise, dt)
        rs_state, rs_cov = es_ekf_predict(state, covariance, process_noise, dt)

        max_es_state_error = max(
            max_es_state_error,
            float(np.max(np.abs(py_state - rs_state))),
        )
        max_es_cov_error = max(
            max_es_cov_error,
            float(np.max(np.abs(py_cov - rs_cov))),
        )

    return ParityResult(
        position_max_abs_error=max_position_error,
        covariance_max_abs_error=max_cov_error,
        es_state_max_abs_error=max_es_state_error,
        es_covariance_max_abs_error=max_es_cov_error,
    )


def main() -> None:
    result = run_parity_tests()
    print("=== Rust vs Python filter math ===")
    print(f"Position Δ (max abs): {result.position_max_abs_error:.3e}")
    print(f"Covariance Δ (max abs): {result.covariance_max_abs_error:.3e}")
    print(f"ES-EKF state Δ (max abs): {result.es_state_max_abs_error:.3e}")
    print(f"ES-EKF covariance Δ (max abs): {result.es_covariance_max_abs_error:.3e}")
    if result.position_max_abs_error < 1e-12 and result.covariance_max_abs_error < 1e-12:
        if result.es_state_max_abs_error < 1e-12 and result.es_covariance_max_abs_error < 1e-12:
            print("\nParity looks good ✅")
        else:
            print("\n⚠️  ES-EKF predict differences detected.")
    else:
        print("\n⚠️  Differences detected. Investigate before trusting the Rust port.")


if __name__ == "__main__":
    main()
def python_kalman_update(
    state: np.ndarray,
    covariance: np.ndarray,
    measurement_matrix: np.ndarray,
    residual: np.ndarray,
    measurement_noise: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    H = measurement_matrix
    R = measurement_noise
    z = residual.reshape(-1, 1)
    x = state.reshape(-1, 1)

    S = H @ covariance @ H.T + R
    try:
        S_inv = np.linalg.inv(S)
    except np.linalg.LinAlgError:
        S = S + 1e-9 * np.eye(S.shape[0])
        S_inv = np.linalg.inv(S)

    K = covariance @ H.T @ S_inv
    dx = K @ z
    x = x + dx

    I_KH = np.eye(covariance.shape[0]) - K @ H
    P = I_KH @ covariance @ I_KH.T + K @ R @ K.T

    return x.flatten(), P
