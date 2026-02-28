package com.gojo.core

import android.content.Context
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock

/**
 * Single-threaded owner of the EKF handle and the session logger.
 *
 * All GojoJni calls happen exclusively on this thread — no locking needed
 * because SensorFusion is single-threaded by design.
 *
 * SensorService registers IMU and GPS callbacks with this thread's looper, so
 * callbacks arrive here directly.  The UI thread reads [cachedState],
 * [rawGpsLat], [rawGpsLon], [hasFirstGps], and [isLogging] via @Volatile.
 *
 * Publishing strategy: cachedState is replaced with a freshly-allocated array
 * every 5 accel samples and on every GPS fix.  The UI thread reads the
 * reference atomically — no partial read is possible.
 *
 * Logging: [SessionLogger] is created in [onLooperPrepared] (on this thread,
 * so file I/O stays off the main thread). EKF state is sampled at 5 Hz via a
 * recurring Handler post. Writers are flushed every 5 s and closed on shutdown.
 */
class SensorThread(context: Context) : HandlerThread("gojo-sensors") {

    // ── Published to UI thread ────────────────────────────────────────────────

    @Volatile var cachedState: DoubleArray = DoubleArray(12)
    @Volatile var rawGpsLat:   Double  = Double.NaN
    @Volatile var rawGpsLon:   Double  = Double.NaN
    @Volatile var hasFirstGps: Boolean = false
    @Volatile var isLogging:   Boolean = false

    // ── Handler for external dispatch (e.g. shutdown from UI thread) ──────────

    lateinit var handler: Handler
        private set

    // ── Private state ─────────────────────────────────────────────────────────

    private val appContext = context.applicationContext

    private var ekfHandle:  Long = 0L
    private var imuCounter: Int  = 0
    private var logger:     SessionLogger? = null

    private var cachedGx = 0.0
    private var cachedGy = 0.0
    private var cachedGz = 0.0

    // ── HandlerThread setup ───────────────────────────────────────────────────

    override fun onLooperPrepared() {
        handler = Handler(looper)

        // Session logger — created here so all file I/O stays on this thread.
        logger = try {
            SessionLogger(appContext)
        } catch (_: Exception) {
            null // external storage unavailable; run without logging
        }

        ekfHandle = GojoJni.init()

        // Apply stationary calibration if CalibrationActivity collected it.
        GojoApp.instance.calibration?.let { cal ->
            GojoJni.setCalibration(
                ekfHandle,
                gx = cal.gx, gy = cal.gy, gz = cal.gz,
                bx = cal.bx, by = cal.by, bz = cal.bz,
            )
        }

        if (logger != null) {
            isLogging = true
            handler.postDelayed(ekfLogRunnable,  EKF_LOG_INTERVAL_MS)
            handler.postDelayed(flushRunnable,    FLUSH_INTERVAL_MS)
        }
    }

    // ── EKF state logger at 5 Hz ──────────────────────────────────────────────

    private val ekfLogRunnable = object : Runnable {
        override fun run() {
            if (ekfHandle != 0L) {
                logger?.logEkf(SystemClock.elapsedRealtimeNanos(), GojoJni.getState(ekfHandle))
            }
            handler.postDelayed(this, EKF_LOG_INTERVAL_MS)
        }
    }

    // ── Periodic writer flush ─────────────────────────────────────────────────

    private val flushRunnable = object : Runnable {
        override fun run() {
            logger?.flush()
            handler.postDelayed(this, FLUSH_INTERVAL_MS)
        }
    }

    // ── Sensor callbacks (called on this thread by SensorService) ─────────────

    /** Feed one accelerometer event into the EKF and logger. */
    fun onAccel(x: Double, y: Double, z: Double, timestampNs: Long) {
        if (ekfHandle == 0L) return
        GojoJni.processImu(
            ekfHandle,
            ax = x, ay = y, az = z,
            gx = cachedGx, gy = cachedGy, gz = cachedGz,
            timestampNs = timestampNs,
        )
        logger?.logAccel(timestampNs, x, y, z)
        if (++imuCounter >= 5) {
            imuCounter  = 0
            cachedState = GojoJni.getState(ekfHandle) // new array → safe publish
        }
    }

    /** Cache the latest gyro reading and log it. */
    fun onGyro(x: Double, y: Double, z: Double, timestampNs: Long) {
        cachedGx = x; cachedGy = y; cachedGz = z
        logger?.logGyro(timestampNs, x, y, z)
    }

    /** Feed one GPS fix into the EKF and logger. */
    fun onGps(
        lat: Double, lon: Double, alt: Double,
        accuracy: Float, speed: Float, bearing: Float,
        timestampNs: Long, satellites: Int,
    ) {
        if (ekfHandle == 0L) return
        GojoJni.processGps(
            ekfHandle,
            lat = lat, lon = lon, alt = alt,
            accuracy = accuracy, speed = speed, bearing = bearing,
            timestampNs = timestampNs,
        )
        logger?.logGps(timestampNs, lat, lon, alt, accuracy, speed, bearing, satellites)
        rawGpsLat   = lat
        rawGpsLon   = lon
        hasFirstGps = true
        cachedState = GojoJni.getState(ekfHandle)
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    /**
     * Flush + close the logger, free the EKF, stop the looper.
     * Safe to call from any thread; work is dispatched onto this looper.
     */
    fun shutdown() {
        if (::handler.isInitialized) {
            handler.post {
                handler.removeCallbacks(ekfLogRunnable)
                handler.removeCallbacks(flushRunnable)
                logger?.close()
                isLogging = false
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

    companion object {
        private const val EKF_LOG_INTERVAL_MS = 200L  // 5 Hz EKF sampling
        private const val FLUSH_INTERVAL_MS   = 5_000L
    }
}
