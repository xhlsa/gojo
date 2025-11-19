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
    private var isRecording = false
    private var sensorCollector: SensorCollector? = null

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

            // Acquire WakeLock
            wakeLock?.acquire(60 * 60 * 1000L) // 1 hour timeout

            // Initialize Rust JNI session
            JniBinding.startSession()
            isRecording = true

            // Start sensor collection
            try {
                sensorCollector = SensorCollector(sensorManager)
                sensorCollector?.start()
                Log.d(tag, "Sensor collection started")
            } catch (e: Exception) {
                Log.e(tag, "Warning: Sensor collection failed (will continue without sensors)", e)
                // Don't stop service, allow inertial-only fallback
            }

            Log.i(tag, "✓ Service running (WakeLock acquired, sensors active)")

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
            // Stop sensor collection
            sensorCollector?.stop()
            sensorCollector = null

            // Stop recording
            if (isRecording) {
                JniBinding.stopSession()
                isRecording = false
            }

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
}
