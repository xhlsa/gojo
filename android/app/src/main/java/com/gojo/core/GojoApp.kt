package com.gojo.core

import android.app.Application

/**
 * Application subclass used as a lightweight cross-Activity holder.
 *
 * Currently carries one piece of state: the [CalibrationData] computed by
 * [CalibrationActivity] before the first drive.  [SensorThread] reads it in
 * [SensorThread.onLooperPrepared] and feeds it to the EKF via
 * [GojoJni.setCalibration].
 *
 * Register in AndroidManifest.xml:
 *   android:name="com.gojo.core.GojoApp"
 */
class GojoApp : Application() {

    /**
     * Gravity vector and gyro bias collected during the stationary calibration
     * preamble.  Null if the user skipped calibration (shouldn't happen with
     * CalibrationActivity as the launcher, but handled gracefully).
     */
    data class CalibrationData(
        val gx: Double, val gy: Double, val gz: Double, // mean accel (gravity vector, m/s²)
        val bx: Double, val by: Double, val bz: Double, // mean gyro  (bias, rad/s)
    )

    var calibration: CalibrationData? = null

    companion object {
        lateinit var instance: GojoApp
            private set
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
    }
}
