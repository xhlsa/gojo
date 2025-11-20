package com.example.motiontracker.data

import android.os.Build
import com.google.gson.annotations.SerializedName

/**
 * Motion tracking session configuration
 *
 * Passed to Rust via JNI as JSON-serialized struct
 * Contains device info and sensor parameters for EKF tuning
 */
data class SessionConfig(
    @SerializedName("device_model")
    val deviceModel: String = Build.MODEL,

    @SerializedName("device_manufacturer")
    val deviceManufacturer: String = Build.MANUFACTURER,

    @SerializedName("os_version")
    val osVersion: Int = Build.VERSION.SDK_INT,

    @SerializedName("accel_rate_hz")
    val accelRateHz: Int = 50,  // ~50 Hz for LSM6DSO

    @SerializedName("gyro_rate_hz")
    val gyroRateHz: Int = 50,   // ~50 Hz for LSM6DSO

    @SerializedName("gps_rate_hz")
    val gpsRateHz: Float = 0.2f, // 1 fix per ~5 seconds

    @SerializedName("ekf_process_noise")
    val ekfProcessNoise: Double = 0.3,  // m/s² for accel drift

    @SerializedName("gps_noise_std")
    val gpsNoiseStd: Double = 8.0,  // meters (GPS accuracy)

    @SerializedName("gyro_noise_std")
    val gyroNoiseStd: Double = 0.0005  // rad/s
) {
    /**
     * Serialize to JSON for JNI passing
     * Uses Gson for consistency with Rust serde_json
     */
    fun toJson(): String {
        return com.google.gson.Gson().toJson(this)
    }

    companion object {
        /**
         * Create default config for current device
         */
        fun default(): SessionConfig = SessionConfig()
    }
}
