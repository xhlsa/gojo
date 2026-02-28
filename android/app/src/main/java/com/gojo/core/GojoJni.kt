package com.gojo.core

/**
 * JNI declarations for the Gojo Rust EKF library.
 *
 * The native library is built by running ./build.sh from the repo root.
 * It compiles motion_tracker_rs as a cdylib and copies the result to
 * android/app/src/main/jniLibs/arm64-v8a/libgojo_core.so.
 *
 * All functions operate on an opaque Long handle returned by [init].
 * The handle is a raw pointer to a Rust GojoState struct — never share
 * it across threads without external synchronisation.
 *
 * Typical lifecycle (Step 3 will expand this):
 *   val handle = GojoJni.init()
 *   // ... feed sensor data ...
 *   GojoJni.destroy(handle)
 */
object GojoJni {

    init {
        System.loadLibrary("gojo_core")
    }

    // ── Session lifecycle ────────────────────────────────────────────────────

    /**
     * Allocate a new EKF session.
     * Returns an opaque handle (raw pointer). Must be freed with [destroy].
     */
    external fun init(): Long

    /**
     * Free the EKF session. Call from onDestroy to avoid memory leaks.
     */
    external fun destroy(handle: Long)

    // ── Calibration ──────────────────────────────────────────────────────────

    /**
     * Set gravity and gyro bias from a stationary calibration preamble.
     *
     * @param gx/gy/gz  Mean accelerometer reading while stationary (m/s²).
     *                  Typically ~(0, 0, 9.81) on a flat surface.
     * @param bx/by/bz  Mean gyroscope reading while stationary (rad/s).
     *                  Typically ~(0, 0, 0).
     *
     * Call after collecting 50+ stationary samples before the first drive.
     */
    external fun setCalibration(
        handle: Long,
        gx: Double, gy: Double, gz: Double,
        bx: Double, by: Double, bz: Double,
    )

    // ── Sensor feeds ─────────────────────────────────────────────────────────

    /**
     * Feed one paired IMU sample (accel + gyro at the same timestamp).
     *
     * @param ax/ay/az      Accelerometer (m/s², body frame).
     * @param gx/gy/gz      Gyroscope (rad/s, body frame).
     * @param timestampNs   SensorEvent.timestamp — elapsedRealtimeNanos.
     * @return              Bitmask of [FLAG_*] constants for notable events.
     *
     * Call at ~100 Hz from the sensor callback thread.
     */
    external fun processImu(
        handle: Long,
        ax: Double, ay: Double, az: Double,
        gx: Double, gy: Double, gz: Double,
        timestampNs: Long,
    ): Int

    /**
     * Feed one GPS fix.
     *
     * @param lat/lon       WGS84 degrees.
     * @param alt           Metres (logged but not fused — no barometer).
     * @param accuracy      Horizontal accuracy (m), from Location.getAccuracy().
     * @param speed         Ground speed (m/s), from Location.getSpeed().
     * @param bearing       Heading (degrees), from Location.getBearing().
     * @param timestampNs   Location.getElapsedRealtimeNanos() — same clock as IMU.
     * @return              Bitmask of [FLAG_*] constants. [FLAG_COLD_START] fires
     *                      on the first accepted fix (origin set).
     *
     * Call at ~1 Hz from the location callback.
     */
    external fun processGps(
        handle: Long,
        lat: Double, lon: Double, alt: Double,
        accuracy: Float, speed: Float, bearing: Float,
        timestampNs: Long,
    ): Int

    // ── State query ──────────────────────────────────────────────────────────

    /**
     * Return the current filter state as a 12-element DoubleArray.
     *
     * Index layout:
     *   [0]  latitude          (degrees WGS84)
     *   [1]  longitude         (degrees WGS84)
     *   [2]  altitude          (metres, EKF up-channel — not absolute)
     *   [3]  speed             (m/s, horizontal magnitude)
     *   [4]  heading           (degrees, 0 = North, clockwise)
     *   [5]  vel_north         (m/s)
     *   [6]  vel_east          (m/s)
     *   [7]  cov_trace         (position uncertainty proxy)
     *   [8]  roughness         (road surface estimate)
     *   [9]  gps_gap_secs      (seconds since last accepted GPS fix)
     *   [10] is_stationary     (1.0 = yes, 0.0 = no)
     *   [11] heading_init      (1.0 = heading locked, 0.0 = not yet)
     *
     * Returns an all-zero array if no GPS fix has been received yet.
     */
    external fun getState(handle: Long): DoubleArray

    // ── Event flag constants ─────────────────────────────────────────────────

    /** First GPS fix received; ENU origin is now set. */
    const val FLAG_COLD_START: Int      = 1 shl 0

    /** Heading aligned to GPS bearing (first high-speed fix). */
    const val FLAG_HEADING_ALIGNED: Int = 1 shl 1

    /** Incident detected (braking / impact / swerve). */
    const val FLAG_INCIDENT: Int        = 1 shl 2

    /** GPS gap mode active; speed clamping applied. */
    const val FLAG_GAP_ACTIVE: Int      = 1 shl 3

    /** GPS gap mode exited (GPS fix received after gap). */
    const val FLAG_GAP_EXITED: Int      = 1 shl 4

    /** GPS fix rejected (accuracy > 50 m or velocity jump). */
    const val FLAG_GPS_REJECTED: Int    = 1 shl 5

    /** Zero-velocity update applied (stationary detection). */
    const val FLAG_ZUPT: Int            = 1 shl 6
}
