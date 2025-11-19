package com.example.motiontracker

import android.util.Log

/**
 * JNI Bridge to Rust motion tracker core
 *
 * Loads libmotion_tracker_jni.so and provides Kotlin methods to:
 * - Control session state (start/stop/pause/resume)
 * - Push sensor samples (accel/gyro/GPS)
 * - Query session status
 *
 * All methods are thread-safe (global session state managed in Rust)
 */
object JniBinding {
    private const val TAG = "MotionTracker.JNI"
    private const val LIB_NAME = "motion_tracker_jni"

    init {
        try {
            System.loadLibrary(LIB_NAME)
            Log.i(TAG, "✓ Loaded libmotion_tracker_jni.so")
        } catch (e: UnsatisfiedLinkError) {
            Log.e(TAG, "✗ Failed to load Rust JNI library", e)
            throw RuntimeException("Cannot initialize motion tracker: Rust library not found", e)
        }
    }

    // ========== Session Control ==========

    /**
     * Start a new recording session
     * Transition: Idle → Recording
     *
     * @throws MotionTrackerException if session already running
     */
    @Throws(MotionTrackerException::class)
    fun startSession() {
        val result = nativeStartSession()
        if (result != 0) {
            throw MotionTrackerException("Failed to start session")
        }
        Log.d(TAG, "Session started")
    }

    /**
     * Stop current recording session and save data
     * Transition: Recording/Paused → Idle
     *
     * @throws MotionTrackerException if session not running
     */
    @Throws(MotionTrackerException::class)
    fun stopSession() {
        val result = nativeStopSession()
        if (result != 0) {
            throw MotionTrackerException("Failed to stop session")
        }
        Log.d(TAG, "Session stopped")
    }

    /**
     * Pause current recording session (data stays in memory)
     * Transition: Recording → Paused
     *
     * @throws MotionTrackerException if not recording
     */
    @Throws(MotionTrackerException::class)
    fun pauseSession() {
        val result = nativePauseSession()
        if (result != 0) {
            throw MotionTrackerException("Failed to pause session")
        }
        Log.d(TAG, "Session paused")
    }

    /**
     * Resume recording (Paused → Recording)
     *
     * @throws MotionTrackerException if not paused
     */
    @Throws(MotionTrackerException::class)
    fun resumeSession() {
        val result = nativeResumeSession()
        if (result != 0) {
            throw MotionTrackerException("Failed to resume session")
        }
        Log.d(TAG, "Session resumed")
    }

    // ========== Sensor Data Push ==========

    /**
     * Push accelerometer sample
     *
     * @param x acceleration in m/s² (X-axis)
     * @param y acceleration in m/s² (Y-axis)
     * @param z acceleration in m/s² (Z-axis)
     * @param timestamp seconds since epoch
     * @throws MotionTrackerException on error
     */
    @Throws(MotionTrackerException::class)
    fun pushAccelSample(x: Double, y: Double, z: Double, timestamp: Double) {
        val result = nativePushAccelSample(x, y, z, timestamp)
        if (result != 0) {
            throw MotionTrackerException("Failed to push accel sample")
        }
    }

    /**
     * Push gyroscope sample
     *
     * @param x angular velocity in rad/s (X-axis)
     * @param y angular velocity in rad/s (Y-axis)
     * @param z angular velocity in rad/s (Z-axis)
     * @param timestamp seconds since epoch
     * @throws MotionTrackerException on error
     */
    @Throws(MotionTrackerException::class)
    fun pushGyroSample(x: Double, y: Double, z: Double, timestamp: Double) {
        val result = nativePushGyroSample(x, y, z, timestamp)
        if (result != 0) {
            throw MotionTrackerException("Failed to push gyro sample")
        }
    }

    /**
     * Push GPS sample
     *
     * @param latitude degrees (-90 to 90)
     * @param longitude degrees (-180 to 180)
     * @param altitude meters above sea level
     * @param accuracy meters (1-sigma horizontal accuracy)
     * @param speed m/s
     * @param bearing degrees (0-359)
     * @param timestamp seconds since epoch
     * @throws MotionTrackerException on error
     */
    @Throws(MotionTrackerException::class)
    fun pushGpsSample(
        latitude: Double,
        longitude: Double,
        altitude: Double,
        accuracy: Double,
        speed: Double,
        bearing: Double,
        timestamp: Double
    ) {
        val result = nativePushGpsSample(
            latitude,
            longitude,
            altitude,
            accuracy,
            speed,
            bearing,
            timestamp
        )
        if (result != 0) {
            throw MotionTrackerException("Failed to push GPS sample")
        }
    }

    // ========== Session Status ==========

    /**
     * Get current session state
     *
     * @return "IDLE", "RECORDING", or "PAUSED"
     * @throws MotionTrackerException on error
     */
    @Throws(MotionTrackerException::class)
    fun getSessionState(): SessionState {
        val stateStr = nativeGetSessionState()
            ?: throw MotionTrackerException("Failed to get session state")
        return SessionState.valueOf(stateStr)
    }

    /**
     * Get sample counts [accel, gyro, gps]
     *
     * @return IntArray with 3 elements
     * @throws MotionTrackerException on error
     */
    @Throws(MotionTrackerException::class)
    fun getSampleCounts(): IntArray {
        return nativeGetSampleCounts()
            ?: throw MotionTrackerException("Failed to get sample counts")
    }

    /**
     * Helper: Get sample counts as labeled object
     */
    fun getSampleCountsLabeled(): SampleCounts {
        val counts = getSampleCounts()
        return SampleCounts(
            accel = counts[0],
            gyro = counts[1],
            gps = counts[2]
        )
    }

    // ========== Session Export ==========

    /**
     * Export session as JSON string
     * Contains all accumulated samples and metadata
     *
     * @return JSON string with session data
     * @throws MotionTrackerException on error
     */
    @Throws(MotionTrackerException::class)
    fun getSessionJson(): String {
        return nativeGetSessionJson()
            ?: throw MotionTrackerException("Failed to export session JSON")
    }

    // ========== Native JNI Functions ==========

    private external fun nativeStartSession(): Int
    private external fun nativeStopSession(): Int
    private external fun nativePauseSession(): Int
    private external fun nativeResumeSession(): Int

    private external fun nativePushAccelSample(x: Double, y: Double, z: Double, timestamp: Double): Int
    private external fun nativePushGyroSample(x: Double, y: Double, z: Double, timestamp: Double): Int
    private external fun nativePushGpsSample(
        latitude: Double,
        longitude: Double,
        altitude: Double,
        accuracy: Double,
        speed: Double,
        bearing: Double,
        timestamp: Double
    ): Int

    private external fun nativeGetSessionState(): String?
    private external fun nativeGetSampleCounts(): IntArray?
    private external fun nativeGetSessionJson(): String?
}

/**
 * Session state (mirrors Rust SessionState enum)
 */
enum class SessionState {
    IDLE,
    RECORDING,
    PAUSED
}

/**
 * Sample counts container
 */
data class SampleCounts(
    val accel: Int,
    val gyro: Int,
    val gps: Int
) {
    val total: Int get() = accel + gyro + gps

    override fun toString(): String {
        return "Accel: $accel, Gyro: $gyro, GPS: $gps"
    }
}

/**
 * Exception thrown when JNI operations fail
 */
class MotionTrackerException(message: String, cause: Throwable? = null) :
    Exception(message, cause)
