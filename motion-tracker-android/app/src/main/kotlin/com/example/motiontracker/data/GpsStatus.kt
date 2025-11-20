package com.example.motiontracker.data

/**
 * Real-time GPS status for UI display
 *
 * Published by LocationCollector via LiveData, observed by MotionTrackerActivity
 * Maps to 🔴 (no lock) / 🟡 (acquiring) / 🟢 (locked) indicators
 */
data class GpsStatus(
    val fixCount: Int = 0,
    val lastFixTimestamp: Long = 0L,  // milliseconds since epoch
    val accuracy: Double? = null,      // meters, null if no fix
    val locked: Boolean = false,
    val provider: String? = null       // "gps" or "network"
) {
    /**
     * Determine lock status indicator
     * 🟢 = has fix with accuracy <= 50m
     * 🟡 = acquiring (no fix yet or accuracy > 50m)
     * 🔴 = no lock (no fixes)
     */
    fun indicator(): String = when {
        locked && accuracy != null && accuracy <= 50.0 -> "🟢"
        fixCount > 0 && accuracy != null && accuracy <= 100.0 -> "🟡"
        else -> "🔴"
    }

    /**
     * Human-readable status line for notification
     * Example: "GPS: 🟢 (12 fixes, 8.2m)" or "GPS: 🔴 (searching)"
     */
    fun statusLine(): String = when {
        locked && accuracy != null -> "🟢 ($fixCount fixes, ${String.format("%.1f", accuracy)}m)"
        fixCount > 0 -> "🟡 ($fixCount fixes, searching)"
        else -> "🔴 (searching)"
    }
}
