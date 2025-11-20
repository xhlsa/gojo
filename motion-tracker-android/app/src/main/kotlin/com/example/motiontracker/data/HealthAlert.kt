package com.example.motiontracker.data

/**
 * Health monitoring alerts published by HealthMonitor
 *
 * Displayed in:
 * - Notification subtitle: "⚠ Gyro offline"
 * - Activity UI: Toast for immediate feedback
 * - LiveData: Activity observes for reactive updates
 */
sealed class HealthAlert {
    abstract val component: String
    abstract val timestamp: Long

    /**
     * Sensor went offline (no data for >10s)
     */
    data class SensorOffline(
        override val component: String,  // "accel", "gyro", "gps"
        override val timestamp: Long = System.currentTimeMillis()
    ) : HealthAlert() {
        override fun toString(): String = "⚠ $component offline"
    }

    /**
     * Sensor came back online (recovery after offline)
     */
    data class SensorRecovery(
        override val component: String,
        override val timestamp: Long = System.currentTimeMillis()
    ) : HealthAlert() {
        override fun toString(): String = "✓ $component recovered"
    }

    /**
     * Fatal error (all sensors dead, Rust core error, etc)
     * Session must stop
     */
    data class FatalError(
        val message: String,
        override val component: String = "system",
        override val timestamp: Long = System.currentTimeMillis()
    ) : HealthAlert() {
        override fun toString(): String = "✗ Fatal: $message"
    }
}
