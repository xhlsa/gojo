package com.example.motiontracker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.SensorManager
import android.location.LocationManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import android.Manifest
import androidx.core.content.ContextCompat
import android.content.pm.PackageManager

/**
 * Motion Tracker Foreground Service
 *
 * Manages motion tracking session with:
 * - Persistent notification (not dismissible)
 * - WakeLock to keep device awake
 * - Sensor data collection (accel, gyro, GPS)
 * - Rust core integration via JNI
 *
 * Lifecycle:
 * - onCreate: Initialize sensors, create WakeLock, setup notification
 * - onStartCommand: Start foreground service, begin recording
 * - onDestroy: Stop recording, release WakeLock, cleanup sensors
 */
class MotionTrackerService : Service() {
    private val tag = "MotionTracker.Service"
    private lateinit var sensorManager: android.hardware.SensorManager
    private lateinit var locationManager: LocationManager
    private var wakeLock: PowerManager.WakeLock? = null
    private var sensorCollector: SensorCollector? = null
    private var locationCollector: LocationCollector? = null
    private var healthMonitor: HealthMonitor? = null

    companion object {
        private const val NOTIFICATION_ID = 1
        private const val NOTIFICATION_CHANNEL_ID = "motion_tracker_channel"
    }

    override fun onCreate() {
        super.onCreate()
        Log.i(tag, "Service created")

        try {
            sensorManager = getSystemService(android.content.Context.SENSOR_SERVICE) as android.hardware.SensorManager
            locationManager = getSystemService(LOCATION_SERVICE) as LocationManager

            // Acquire WakeLock (prevent device sleep)
            val powerManager = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "MotionTracker::TrackingWakeLock"
            )

            createNotificationChannel()
        } catch (e: Exception) {
            Log.e(tag, "Failed to initialize service", e)
            stopSelf()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(tag, "Service started")

        try {
            // Create notification and start foreground service
            val notification = buildNotification("Starting...")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                ServiceCompat.startForeground(
                    this,
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }

            // Acquire WakeLock (hold indefinitely until service stops)
            // For extended sessions >8 hours, consider implementing renewal timer
            // Current timeout of 8 hours covers >99% of real-world scenarios
            wakeLock?.acquire(8 * 60 * 60 * 1000L) // 8 hour timeout (covers long drives)

            // Note: JNI session state is controlled by Activity (start/stop/pause/resume buttons)
            // Service only manages infrastructure: sensors, GPS, WakeLock, health monitoring
            // This prevents Activity-Service double-start race condition

            // Start sensor collection
            try {
                sensorCollector = SensorCollector(sensorManager)
                sensorCollector?.start()
                Log.d(tag, "Sensor collection started")
            } catch (e: Exception) {
                Log.e(tag, "Warning: Sensor collection failed (will continue without sensors)", e)
                // Don't stop service, allow inertial-only fallback
            }

            // Start location collection (check permissions first)
            try {
                if (hasLocationPermissions()) {
                    locationCollector = LocationCollector(this, locationManager)
                    locationCollector?.start()
                    Log.d(tag, "Location collection started")
                } else {
                    Log.w(tag, "Location permissions not granted - GPS disabled (inertial-only tracking)")
                }
            } catch (e: Exception) {
                Log.e(tag, "Warning: Location collection failed (will continue without GPS)", e)
                // Don't stop service, allow inertial-only fallback
            }

            // Start health monitoring (auto-restart on sensor silence)
            try {
                healthMonitor = HealthMonitor(this, this)
                healthMonitor?.start()
                Log.d(tag, "Health monitor started")
            } catch (e: Exception) {
                Log.e(tag, "Warning: Health monitor failed", e)
                // Don't stop service, continue without health monitoring
            }

            Log.i(tag, "✓ Service running (WakeLock acquired, sensors + GPS + health monitor active)")

            return START_STICKY  // Restart if killed
        } catch (e: Exception) {
            Log.e(tag, "Failed to start service", e)
            stopSelf()
            return START_NOT_STICKY
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.i(tag, "Service destroyed")

        try {
            // Stop health monitoring
            healthMonitor?.stop()
            healthMonitor = null

            // Stop sensor collection
            sensorCollector?.stop()
            sensorCollector = null

            // Stop location collection
            locationCollector?.stop()
            locationCollector = null

            // Note: JNI session stop is handled by Activity (stopSession via button)
            // Service only manages infrastructure cleanup

            // Release WakeLock
            wakeLock?.let {
                if (it.isHeld) {
                    it.release()
                }
            }

            Log.i(tag, "✓ Service cleaned up")
        } catch (e: Exception) {
            Log.e(tag, "Error during cleanup", e)
        }
    }

    override fun onBind(intent: Intent?): IBinder? {
        return null  // Not a bound service
    }

    /**
     * Create notification channel (required for Android 8+)
     */
    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                "Motion Tracker",
                NotificationManager.IMPORTANCE_DEFAULT
            ).apply {
                description = "Motion tracker recording session"
                enableVibration(false)
            }

            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager?.createNotificationChannel(channel)
        }
    }

    /**
     * Build notification with current session status
     */
    private fun buildNotification(status: String): Notification {
        val intent = Intent(this, MotionTrackerActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val counts = try {
            JniBinding.getSampleCountsLabeled()
        } catch (e: Exception) {
            SampleCounts(0, 0, 0)
        }

        return NotificationCompat.Builder(this, NOTIFICATION_CHANNEL_ID)
            .setContentTitle("Motion Tracker")
            .setContentText(
                "Recording • Accel: ${counts.accel} • GPS: ${counts.gps}"
            )
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(pendingIntent)
            .setOngoing(true)  // Not dismissible
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
    }


    /**
     * Update notification with current status
     */
    private fun updateNotification() {
        try {
            val notification = buildNotification("Recording")
            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager?.notify(NOTIFICATION_ID, notification)
        } catch (e: Exception) {
            Log.e(tag, "Failed to update notification", e)
        }
    }

    /**
     * Update notification with current sample counts
     * Called by HealthMonitor every 2 seconds
     */
    fun updateNotificationWithCounts() {
        try {
            val counts = JniBinding.getSampleCountsLabeled()
            val health = healthMonitor?.getHealthStatus()

            val contentText = if (health != null && !health.isHealthy) {
                // Show warning if health issues
                "⚠ Accel: ${counts.accel} • GPS: ${counts.gps} (${health.summary()})"
            } else {
                // Normal status
                "Recording • Accel: ${counts.accel} • Gyro: ${counts.gyro} • GPS: ${counts.gps}"
            }

            val intent = Intent(this, MotionTrackerActivity::class.java)
            val pendingIntent = PendingIntent.getActivity(
                this,
                0,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val notification = NotificationCompat.Builder(this, NOTIFICATION_CHANNEL_ID)
                .setContentTitle("Motion Tracker")
                .setContentText(contentText)
                .setSmallIcon(android.R.drawable.ic_media_play)
                .setContentIntent(pendingIntent)
                .setOngoing(true)  // Not dismissible
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .build()

            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager?.notify(NOTIFICATION_ID, notification)
        } catch (e: Exception) {
            Log.e(tag, "Failed to update notification with counts", e)
        }
    }

    /**
     * Restart sensor collection (called by HealthMonitor)
     * Gracefully stops and restarts accel/gyro collectors
     */
    fun restartSensorCollection() {
        try {
            Log.w(tag, "Restarting sensor collection...")

            // Stop current collector
            sensorCollector?.stop()
            Thread.sleep(500)  // Wait for cleanup

            // Restart
            sensorCollector = SensorCollector(sensorManager)
            sensorCollector?.start()

            Log.i(tag, "✓ Sensor collection restarted")
        } catch (e: Exception) {
            Log.e(tag, "Failed to restart sensor collection", e)
            throw e
        }
    }

    /**
     * Restart location collection (called by HealthMonitor)
     * Gracefully stops and restarts GPS collector
     */
    fun restartLocationCollection() {
        try {
            Log.w(tag, "Restarting location collection...")

            // Stop current collector
            locationCollector?.stop()
            Thread.sleep(500)  // Wait for cleanup

            // Restart
            locationCollector = LocationCollector(this, locationManager)
            locationCollector?.start()

            Log.i(tag, "✓ Location collection restarted")
        } catch (e: Exception) {
            Log.e(tag, "Failed to restart location collection", e)
            throw e
        }
    }

    /**
     * Get health status from monitor
     */
    fun getHealthStatus(): HealthStatus? {
        return healthMonitor?.getHealthStatus()
    }

    /**
     * Check if location permissions are granted
     */
    private fun hasLocationPermissions(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED &&
        ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
    }
}
