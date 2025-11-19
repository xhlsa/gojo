package com.example.motiontracker

import android.app.NotificationManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Toast
import androidx.core.app.NotificationCompat
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.Executors
import kotlin.math.min

/**
 * Health Monitor for sensor collectors
 *
 * Periodically checks:
 * - Sensor collection active (accel/gyro samples increasing)
 * - Location collection active (GPS fixes increasing)
 * - Auto-restart on silence (> 5 sec without data)
 *
 * Features:
 * - Exponential backoff on repeated failures (1s, 2s, 4s, 8s, 16s max)
 * - Toast notifications on errors
 * - Real-time notification updates with sample counts
 * - Thread-safe monitoring loop
 */
class HealthMonitor(
    private val context: Context,
    private val service: MotionTrackerService
) {
    private val tag = "MotionTracker.Health"

    // Health check state (thread-safe using AtomicInteger)
    // These are accessed by both monitoring thread and service restart threads
    private val lastAccelCount = AtomicInteger(0)
    private val lastGyroCount = AtomicInteger(0)
    private val lastGpsCount = AtomicInteger(0)

    private var lastAccelTime = System.currentTimeMillis()
    private var lastGyroTime = System.currentTimeMillis()
    private var lastGpsTime = System.currentTimeMillis()

    // Failure tracking for exponential backoff (thread-safe)
    private val accelFailures = AtomicInteger(0)
    private val gyroFailures = AtomicInteger(0)
    private val gpsFailures = AtomicInteger(0)

    // Circuit breaker: track total restart attempts to prevent infinite retry loops
    private val accelRestartAttempts = AtomicInteger(0)
    private val gyroRestartAttempts = AtomicInteger(0)
    private val gpsRestartAttempts = AtomicInteger(0)

    private var isMonitoring = false
    private var monitorThread: Thread? = null
    private var restartExecutor = Executors.newFixedThreadPool(2)  // Max 2 concurrent restarts

    companion object {
        private const val HEALTH_CHECK_INTERVAL_MS = 2000L  // Check every 2 seconds
        private const val SENSOR_SILENCE_THRESHOLD_MS = 5000L  // 5 seconds
        private const val MAX_RESTART_BACKOFF_MS = 16000L  // 16 second max backoff
        private const val NOTIFICATION_UPDATE_INTERVAL_MS = 2000L  // Update notification every 2s
        private const val MAX_RESTART_ATTEMPTS = 10  // Circuit breaker: stop retrying after 10 attempts
    }

    /**
     * Start health monitoring
     */
    fun start() {
        if (isMonitoring) {
            Log.w(tag, "Health monitor already running")
            return
        }

        isMonitoring = true

        // Recreate executor if it was previously shut down
        if (restartExecutor.isShutdown) {
            try {
                // Ensure old executor is fully cleaned up before creating new one
                if (!restartExecutor.awaitTermination(500, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                    restartExecutor.shutdownNow()
                }
            } catch (e: Exception) {
                Log.w(tag, "Error cleaning up old restart executor", e)
            }
            restartExecutor = Executors.newFixedThreadPool(2)
            Log.d(tag, "Created new restart executor")
        }

        lastAccelTime = System.currentTimeMillis()
        lastGyroTime = System.currentTimeMillis()
        lastGpsTime = System.currentTimeMillis()

        monitorThread = Thread {
            try {
                monitoringLoop()
            } catch (e: Exception) {
                Log.e(tag, "Health monitor thread crashed", e)
            }
        }.apply {
            name = "MotionTracker-HealthMonitor"
            isDaemon = true
            start()
        }

        Log.i(tag, "✓ Health monitor started")
    }

    /**
     * Stop health monitoring
     */
    fun stop() {
        isMonitoring = false
        monitorThread?.interrupt()
        monitorThread?.join(2000)  // Wait up to 2 seconds
        monitorThread = null

        // Shutdown restart executor thread pool
        try {
            restartExecutor.shutdown()  // Prevent new tasks
            if (!restartExecutor.awaitTermination(2000, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                restartExecutor.shutdownNow()  // Force shutdown after timeout
            }
        } catch (e: Exception) {
            Log.e(tag, "Error shutting down restart executor", e)
        }

        Log.i(tag, "Health monitor stopped")
    }

    /**
     * Main monitoring loop - runs every 2 seconds
     */
    private fun monitoringLoop() {
        var lastNotificationUpdate = System.currentTimeMillis()

        while (isMonitoring) {
            try {
                val now = System.currentTimeMillis()

                // Health checks
                checkSensorHealth(now)
                checkLocationHealth(now)

                // Update notification every 2 seconds
                if (now - lastNotificationUpdate >= NOTIFICATION_UPDATE_INTERVAL_MS) {
                    updateServiceNotification()
                    lastNotificationUpdate = now
                }

                // Sleep before next check
                Thread.sleep(HEALTH_CHECK_INTERVAL_MS)
            } catch (e: InterruptedException) {
                // Expected when stopping
                break
            } catch (e: Exception) {
                Log.e(tag, "Error in health check loop", e)
                Thread.sleep(1000)  // Back off before retrying
            }
        }
    }

    /**
     * Check sensor (accel/gyro) health and auto-restart if silent
     */
    private fun checkSensorHealth(now: Long) {
        try {
            val counts = JniBinding.getSampleCountsLabeled()

            // Check accel
            if (counts.accel > lastAccelCount.get()) {
                // Accel producing data
                lastAccelCount.set(counts.accel)
                lastAccelTime = now
                if (accelFailures.get() > 0) {
                    Log.d(tag, "Accel recovered (${counts.accel} samples)")
                    accelFailures.set(0)
                    accelRestartAttempts.set(0)  // Reset circuit breaker on recovery
                }
            } else if (now - lastAccelTime > SENSOR_SILENCE_THRESHOLD_MS) {
                // Accel silent for > 5 seconds
                accelFailures.incrementAndGet()

                // Check circuit breaker before attempting restart
                if (accelRestartAttempts.get() >= MAX_RESTART_ATTEMPTS) {
                    Log.e(tag, "⚠ Accel circuit breaker triggered (${accelRestartAttempts.get()}/$MAX_RESTART_ATTEMPTS attempts) - giving up")
                } else {
                    val backoffMs = calculateBackoff(accelFailures.get())
                    Log.w(tag, "⚠ Accel silent for ${(now - lastAccelTime)/1000}s (failure ${accelFailures.get()}, restart attempt ${accelRestartAttempts.get() + 1}/$MAX_RESTART_ATTEMPTS, backoff ${backoffMs}ms)")

                    // Attempt restart after backoff
                    accelRestartAttempts.incrementAndGet()
                    scheduleRestart(accelFailures.get()) {
                        restartSensorCollection()
                        lastAccelTime = System.currentTimeMillis()
                        // Get fresh counts after restart (don't use stale outer scope value)
                        try {
                            val freshCounts = JniBinding.getSampleCountsLabeled()
                            lastAccelCount.set(freshCounts.accel)
                        } catch (e: Exception) {
                            Log.e(tag, "Error getting counts after accel restart", e)
                        }
                    }
                }
            }

            // Check gyro
            if (counts.gyro > lastGyroCount.get()) {
                // Gyro producing data
                lastGyroCount.set(counts.gyro)
                lastGyroTime = now
                if (gyroFailures.get() > 0) {
                    Log.d(tag, "Gyro recovered (${counts.gyro} samples)")
                    gyroFailures.set(0)
                    gyroRestartAttempts.set(0)  // Reset circuit breaker on recovery
                }
            } else if (now - lastGyroTime > SENSOR_SILENCE_THRESHOLD_MS) {
                // Gyro silent for > 5 seconds
                gyroFailures.incrementAndGet()

                // Check circuit breaker before attempting restart
                if (gyroRestartAttempts.get() >= MAX_RESTART_ATTEMPTS) {
                    Log.e(tag, "⚠ Gyro circuit breaker triggered (${gyroRestartAttempts.get()}/$MAX_RESTART_ATTEMPTS attempts) - giving up")
                } else {
                    val backoffMs = calculateBackoff(gyroFailures.get())
                    Log.w(tag, "⚠ Gyro silent for ${(now - lastGyroTime)/1000}s (failure ${gyroFailures.get()}, restart attempt ${gyroRestartAttempts.get() + 1}/$MAX_RESTART_ATTEMPTS, backoff ${backoffMs}ms)")

                    // Attempt restart after backoff
                    gyroRestartAttempts.incrementAndGet()
                    scheduleRestart(gyroFailures.get()) {
                        restartSensorCollection()
                        lastGyroTime = System.currentTimeMillis()
                        // Get fresh counts after restart (don't use stale outer scope value)
                        try {
                            val freshCounts = JniBinding.getSampleCountsLabeled()
                            lastGyroCount.set(freshCounts.gyro)
                        } catch (e: Exception) {
                            Log.e(tag, "Error getting counts after gyro restart", e)
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(tag, "Error checking sensor health", e)
        }
    }

    /**
     * Check location (GPS) health and auto-restart if silent
     */
    private fun checkLocationHealth(now: Long) {
        try {
            val counts = JniBinding.getSampleCountsLabeled()

            if (counts.gps > lastGpsCount.get()) {
                // GPS producing data
                lastGpsCount.set(counts.gps)
                lastGpsTime = now
                if (gpsFailures.get() > 0) {
                    Log.d(tag, "GPS recovered (${counts.gps} fixes)")
                    gpsFailures.set(0)
                    gpsRestartAttempts.set(0)  // Reset circuit breaker on recovery
                }
            } else if (now - lastGpsTime > SENSOR_SILENCE_THRESHOLD_MS) {
                // GPS silent for > 5 seconds
                gpsFailures.incrementAndGet()

                // Check circuit breaker before attempting restart
                if (gpsRestartAttempts.get() >= MAX_RESTART_ATTEMPTS) {
                    Log.e(tag, "⚠ GPS circuit breaker triggered (${gpsRestartAttempts.get()}/$MAX_RESTART_ATTEMPTS attempts) - giving up")
                } else {
                    val backoffMs = calculateBackoff(gpsFailures.get())
                    Log.w(tag, "⚠ GPS silent for ${(now - lastGpsTime)/1000}s (failure ${gpsFailures.get()}, restart attempt ${gpsRestartAttempts.get() + 1}/$MAX_RESTART_ATTEMPTS, backoff ${backoffMs}ms)")

                    // Attempt restart after backoff
                    gpsRestartAttempts.incrementAndGet()
                    scheduleRestart(gpsFailures.get()) {
                        restartLocationCollection()
                        lastGpsTime = System.currentTimeMillis()
                        // Get fresh counts after restart (don't use stale outer scope value)
                        try {
                            val freshCounts = JniBinding.getSampleCountsLabeled()
                            lastGpsCount.set(freshCounts.gps)
                        } catch (e: Exception) {
                            Log.e(tag, "Error getting counts after GPS restart", e)
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(tag, "Error checking location health", e)
        }
    }

    /**
     * Restart sensor collection with error feedback
     */
    private fun restartSensorCollection() {
        try {
            Log.i(tag, "Restarting sensor collection...")
            service.restartSensorCollection()
            showToast("Sensors restarted")
        } catch (e: Exception) {
            Log.e(tag, "Failed to restart sensors", e)
            showToast("⚠ Sensor restart failed: ${e.message}")
        }
    }

    /**
     * Restart location collection with error feedback
     */
    private fun restartLocationCollection() {
        try {
            Log.i(tag, "Restarting location collection...")
            service.restartLocationCollection()
            showToast("GPS restarted")
        } catch (e: Exception) {
            Log.e(tag, "Failed to restart location", e)
            showToast("⚠ GPS restart failed: ${e.message}")
        }
    }

    /**
     * Schedule restart with exponential backoff (non-blocking)
     * Uses per-sensor failure count for correct backoff timing
     * Uses Handler.postDelayed instead of blocking sleep
     */
    private fun scheduleRestart(sensorFailureCount: Int, action: () -> Unit) {
        val backoffMs = calculateBackoff(sensorFailureCount)
        Log.d(tag, "Scheduling restart in ${backoffMs}ms (failures=$sensorFailureCount)")

        // Use Handler.postDelayed for non-blocking restart (doesn't block executor thread)
        Handler(Looper.getMainLooper()).postDelayed({
            try {
                // Check isMonitoring before executing (prevent post-stop restarts)
                if (isMonitoring) {
                    action()
                } else {
                    Log.d(tag, "Monitoring stopped, skipping restart")
                }
            } catch (e: Exception) {
                Log.e(tag, "Error in scheduled restart", e)
            }
        }, backoffMs)
    }

    /**
     * Calculate exponential backoff: 1s, 2s, 4s, 8s, 16s (max)
     */
    private fun calculateBackoff(failureCount: Int): Long {
        val baseMs = 1000L
        val factor = 1 shl (failureCount - 1)  // 2^(n-1)
        return min(baseMs * factor, MAX_RESTART_BACKOFF_MS)
    }

    /**
     * Update service notification with current sample counts
     */
    private fun updateServiceNotification() {
        try {
            service.updateNotificationWithCounts()
        } catch (e: Exception) {
            Log.e(tag, "Failed to update notification", e)
        }
    }

    /**
     * Show toast notification to user
     */
    private fun showToast(message: String) {
        try {
            // Use Handler to ensure toast runs on main thread
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
            }
            Log.i(tag, "Toast: $message")
        } catch (e: Exception) {
            Log.e(tag, "Failed to show toast", e)
        }
    }

    /**
     * Get health status summary
     */
    fun getHealthStatus(): HealthStatus {
        return HealthStatus(
            accelFailures = accelFailures.get(),
            gyroFailures = gyroFailures.get(),
            gpsFailures = gpsFailures.get(),
            accelSilenceMs = System.currentTimeMillis() - lastAccelTime,
            gyroSilenceMs = System.currentTimeMillis() - lastGyroTime,
            gpsSilenceMs = System.currentTimeMillis() - lastGpsTime,
            isHealthy = accelFailures.get() == 0 && gyroFailures.get() == 0 && gpsFailures.get() == 0
        )
    }
}

/**
 * Health status snapshot
 */
data class HealthStatus(
    val accelFailures: Int,
    val gyroFailures: Int,
    val gpsFailures: Int,
    val accelSilenceMs: Long,
    val gyroSilenceMs: Long,
    val gpsSilenceMs: Long,
    val isHealthy: Boolean
) {
    fun summary(): String {
        return "Accel:$accelFailures Gyro:$gyroFailures GPS:$gpsFailures" +
               " (silence: accel=${accelSilenceMs/1000}s gyro=${gyroSilenceMs/1000}s gps=${gpsSilenceMs/1000}s)"
    }
}
