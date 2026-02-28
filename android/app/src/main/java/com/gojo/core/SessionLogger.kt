package com.gojo.core

import android.content.Context
import android.os.Build
import android.os.SystemClock
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Writes three CSVs + a metadata file for each sensor session.
 *
 * All methods are called exclusively from the sensor thread — no locking needed.
 *
 * File layout inside [sessionDir]:
 *   imu.csv      — elapsed_nanos, sensor_type, x, y, z
 *   gps.csv      — elapsed_nanos, lat, lon, alt, accuracy_m, speed_mps, bearing_deg, satellites
 *   ekf.csv      — elapsed_nanos, lat, lon, alt, speed, heading, vn, ve,
 *                  cov_trace, roughness, gps_gap, is_stationary, heading_initialized
 *   metadata.txt — key=value device info
 *
 * The imu/gps/ekf formats are compatible with the existing Python tools
 * (verify_data.py, generate_gpx_for_comparison.py).
 *
 * Writers are buffered (64 KiB for IMU, 8 KiB for GPS/EKF).
 * Call [flush] periodically (every ~5 s) and [close] at session end.
 */
class SessionLogger(context: Context) {

    val sessionDir: File

    @Volatile var isActive: Boolean = false
        private set

    private val imuWriter: BufferedWriter
    private val gpsWriter: BufferedWriter
    private val ekfWriter: BufferedWriter

    init {
        val appCtx    = context.applicationContext
        val fmt       = SimpleDateFormat("yyyy-MM-dd_HH-mm-ss", Locale.US)
        val timestamp = fmt.format(Date())

        val baseDir = File(appCtx.getExternalFilesDir(null), "Gojo/sessions/$timestamp")
        baseDir.mkdirs()
        sessionDir = baseDir

        imuWriter = BufferedWriter(FileWriter(File(baseDir, "imu.csv")), 65_536)
        gpsWriter = BufferedWriter(FileWriter(File(baseDir, "gps.csv")),  8_192)
        ekfWriter = BufferedWriter(FileWriter(File(baseDir, "ekf.csv")), 16_384)

        imuWriter.write("elapsed_nanos,sensor_type,x,y,z\n")
        gpsWriter.write("elapsed_nanos,lat,lon,alt,accuracy_m,speed_mps,bearing_deg,satellites\n")
        ekfWriter.write("elapsed_nanos,lat,lon,alt,speed,heading,vn,ve," +
                        "cov_trace,roughness,gps_gap,is_stationary,heading_initialized\n")

        writeMetadata(appCtx, baseDir)
        isActive = true
    }

    // ── Log methods ───────────────────────────────────────────────────────────

    fun logAccel(timestampNs: Long, x: Double, y: Double, z: Double) {
        imuWriter.write("$timestampNs,accel,$x,$y,$z\n")
    }

    fun logGyro(timestampNs: Long, x: Double, y: Double, z: Double) {
        imuWriter.write("$timestampNs,gyro,$x,$y,$z\n")
    }

    fun logGps(
        timestampNs: Long,
        lat: Double, lon: Double, alt: Double,
        accuracy: Float, speed: Float, bearing: Float,
        satellites: Int,
    ) {
        gpsWriter.write("$timestampNs,$lat,$lon,$alt,$accuracy,$speed,$bearing,$satellites\n")
    }

    /**
     * Log the current EKF state (12-element array from [GojoJni.getState]).
     * Called at 5 Hz from the periodic timer in [SensorThread].
     */
    fun logEkf(timestampNs: Long, state: DoubleArray) {
        if (state.size < 12) return
        ekfWriter.write(
            "$timestampNs," +
            "${state[0]},${state[1]},${state[2]},${state[3]},${state[4]}," +
            "${state[5]},${state[6]},${state[7]},${state[8]},${state[9]}," +
            "${state[10]},${state[11]}\n"
        )
    }

    // ── I/O control ───────────────────────────────────────────────────────────

    fun flush() {
        try {
            imuWriter.flush()
            gpsWriter.flush()
            ekfWriter.flush()
        } catch (_: Exception) {}
    }

    fun close() {
        isActive = false
        try {
            flush()
            imuWriter.close()
            gpsWriter.close()
            ekfWriter.close()
        } catch (_: Exception) {}
    }

    // ── Metadata ──────────────────────────────────────────────────────────────

    private fun writeMetadata(context: Context, dir: File) {
        val utcFmt = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US).apply {
            timeZone = TimeZone.getTimeZone("UTC")
        }
        val sessionStartUtcMs = System.currentTimeMillis()
        // elapsedRealtime() is ms since boot; difference gives boot→UTC offset.
        val bootOffsetMs = sessionStartUtcMs - SystemClock.elapsedRealtime()

        File(dir, "metadata.txt").writeText(buildString {
            appendLine("device=${Build.MANUFACTURER} ${Build.MODEL}")
            appendLine("android_version=${Build.VERSION.RELEASE}")
            appendLine("session_start_utc_ms=$sessionStartUtcMs")
            appendLine("boot_offset_ms=$bootOffsetMs")
            append    ("session_start_utc=${utcFmt.format(Date(sessionStartUtcMs))}")
        })
    }
}
