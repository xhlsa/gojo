package com.gojo.core

import android.os.Handler
import android.os.HandlerThread

/**
 * Single-threaded owner of the EKF handle.
 *
 * All GojoJni calls happen exclusively on this thread — no locking needed
 * because SensorFusion is single-threaded by design.
 *
 * SensorService registers IMU and GPS callbacks with this thread's looper, so
 * callbacks arrive here directly.  The UI thread reads [cachedState],
 * [rawGpsLat], [rawGpsLon], and [hasFirstGps] via @Volatile references.
 *
 * Publishing strategy: cachedState is replaced with a freshly-allocated array
 * every 5 accel samples and on every GPS fix.  The UI thread reads the
 * reference atomically — no partial read is possible.
 */
class SensorThread : HandlerThread("gojo-sensors") {

    // ── Published to UI thread (read by Handler post at 200 ms) ──────────────

    @Volatile var cachedState: DoubleArray = DoubleArray(12)
    @Volatile var rawGpsLat:   Double = Double.NaN
    @Volatile var rawGpsLon:   Double = Double.NaN
    @Volatile var hasFirstGps: Boolean = false

    // ── Handler for UI-→thread dispatch (e.g. shutdown) ──────────────────────

    lateinit var handler: Handler
        private set

    // ── Private EKF state ─────────────────────────────────────────────────────

    private var ekfHandle: Long = 0L
    private var imuCounter:  Int = 0

    // Latest gyro reading — cached so it is paired with the next accel event.
    private var cachedGx = 0.0
    private var cachedGy = 0.0
    private var cachedGz = 0.0

    // ── HandlerThread setup ───────────────────────────────────────────────────

    override fun onLooperPrepared() {
        handler    = Handler(looper)
        ekfHandle  = GojoJni.init()
    }

    // ── Sensor callbacks (called on this thread by SensorService) ─────────────

    /** Feed one accelerometer event into the filter. */
    fun onAccel(x: Double, y: Double, z: Double, timestampNs: Long) {
        if (ekfHandle == 0L) return
        GojoJni.processImu(
            ekfHandle,
            ax = x, ay = y, az = z,
            gx = cachedGx, gy = cachedGy, gz = cachedGz,
            timestampNs = timestampNs,
        )
        if (++imuCounter >= 5) {
            imuCounter  = 0
            cachedState = GojoJni.getState(ekfHandle) // new array → safe publish
        }
    }

    /** Cache the latest gyro reading (paired with the next accel event). */
    fun onGyro(x: Double, y: Double, z: Double) {
        cachedGx = x; cachedGy = y; cachedGz = z
    }

    /** Feed one GPS fix into the filter and update raw-GPS volatiles. */
    fun onGps(
        lat: Double, lon: Double, alt: Double,
        accuracy: Float, speed: Float, bearing: Float,
        timestampNs: Long,
    ) {
        if (ekfHandle == 0L) return
        GojoJni.processGps(
            ekfHandle,
            lat = lat, lon = lon, alt = alt,
            accuracy = accuracy, speed = speed, bearing = bearing,
            timestampNs = timestampNs,
        )
        rawGpsLat   = lat
        rawGpsLon   = lon
        hasFirstGps = true
        cachedState = GojoJni.getState(ekfHandle) // always refresh after GPS
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /**
     * Destroy the EKF and stop the looper.
     * Safe to call from any thread; work is dispatched to this thread's looper.
     */
    fun shutdown() {
        if (::handler.isInitialized) {
            handler.post {
                if (ekfHandle != 0L) {
                    GojoJni.destroy(ekfHandle)
                    ekfHandle = 0L
                }
                quitSafely()
            }
        } else {
            quitSafely()
        }
    }
}
