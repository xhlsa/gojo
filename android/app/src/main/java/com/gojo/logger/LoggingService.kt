package com.gojo.logger

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.Location
import android.os.Binder
import android.os.Environment
import android.os.IBinder
import android.os.SystemClock
import android.util.Log
import com.google.android.gms.location.*
import java.io.BufferedWriter
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.*

class LoggingService : Service(), SensorEventListener {

    companion object {
        private const val TAG = "GojoLogger"
        private const val CHANNEL_ID = "gojo_logging"
        private const val NOTIFICATION_ID = 1

        // IMU at ~100Hz (10ms). Adjust if needed.
        private const val IMU_PERIOD_US = 10_000

        // GPS at 1Hz
        private const val GPS_INTERVAL_MS = 1000L
    }

    inner class LocalBinder : Binder() {
        fun getService(): LoggingService = this@LoggingService
    }

    private val binder = LocalBinder()

    private lateinit var sensorManager: SensorManager
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private var locationCallback: LocationCallback? = null

    private var imuWriter: BufferedWriter? = null
    private var gpsWriter: BufferedWriter? = null

    var isLogging = false
        private set

    var imuSampleCount: Long = 0
        private set

    var gpsSampleCount: Long = 0
        private set

    var startTimeNanos: Long = 0
        private set

    var outputDir: String? = null
        private set

    // Offset: wall clock reference for post-processing
    // Captured once at start so you can map elapsedRealtimeNanos -> UTC
    private var bootTimeUtcMs: Long = 0

    override fun onBind(intent: Intent?): IBinder = binder

    override fun onCreate() {
        super.onCreate()
        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        createNotificationChannel()
    }

    fun startLogging() {
        if (isLogging) return

        // Compute UTC offset: wall_clock_ms - elapsed_ms
        // This lets you convert any elapsedRealtimeNanos to UTC in post-processing
        bootTimeUtcMs = System.currentTimeMillis() - SystemClock.elapsedRealtime()

        isLogging = true
        imuSampleCount = 0
        gpsSampleCount = 0
        startTimeNanos = SystemClock.elapsedRealtimeNanos()

        openFiles()
        startIMU()
        startGPS()

        // Foreground notification
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Gojo Logger")
            .setContentText("Recording sensors...")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setOngoing(true)
            .build()

        startForeground(NOTIFICATION_ID, notification)
        Log.i(TAG, "Logging started. Output: $outputDir")
    }

    fun stopLogging() {
        if (!isLogging) return
        isLogging = false

        sensorManager.unregisterListener(this)
        locationCallback?.let { fusedLocationClient.removeLocationUpdates(it) }

        imuWriter?.flush()
        imuWriter?.close()
        gpsWriter?.flush()
        gpsWriter?.close()

        stopForeground(STOP_FOREGROUND_REMOVE)
        Log.i(TAG, "Logging stopped. IMU=$imuSampleCount GPS=$gpsSampleCount")
    }

    // --- File setup ---

    private fun openFiles() {
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val dir = File(
            getExternalFilesDir(Environment.DIRECTORY_DOCUMENTS),
            "gojo_$timestamp"
        )
        dir.mkdirs()
        outputDir = dir.absolutePath

        // Write a metadata file with the clock offset
        File(dir, "metadata.txt").writeText(buildString {
            appendLine("session_start_utc_ms=${ System.currentTimeMillis() }")
            appendLine("boot_time_utc_ms=$bootTimeUtcMs")
            appendLine("start_elapsed_nanos=$startTimeNanos")
            appendLine("imu_period_us=$IMU_PERIOD_US")
            appendLine("gps_interval_ms=$GPS_INTERVAL_MS")
            appendLine("device=${android.os.Build.MODEL}")
            appendLine("android_sdk=${android.os.Build.VERSION.SDK_INT}")
        })

        // IMU CSV: elapsed_nanos, accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z
        val imuFile = File(dir, "imu.csv")
        imuWriter = BufferedWriter(FileWriter(imuFile))
        imuWriter?.write("elapsed_nanos,sensor_type,x,y,z,accuracy\n")

        // GPS CSV: elapsed_nanos, lat, lon, alt, accuracy, speed, bearing
        val gpsFile = File(dir, "gps.csv")
        gpsWriter = BufferedWriter(FileWriter(gpsFile))
        gpsWriter?.write("elapsed_nanos,lat,lon,alt_m,accuracy_m,speed_mps,bearing_deg,satellites\n")
    }

    // --- IMU ---

    private fun startIMU() {
        val accel = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        val gyro = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)

        accel?.let {
            sensorManager.registerListener(this, it, IMU_PERIOD_US)
            Log.i(TAG, "Accelerometer registered")
        } ?: Log.w(TAG, "No accelerometer found!")

        gyro?.let {
            sensorManager.registerListener(this, it, IMU_PERIOD_US)
            Log.i(TAG, "Gyroscope registered")
        } ?: Log.w(TAG, "No gyroscope found!")
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (!isLogging) return

        val type = when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> "accel"
            Sensor.TYPE_GYROSCOPE -> "gyro"
            else -> return
        }

        // event.timestamp is elapsedRealtimeNanos on most devices (API 26+)
        val line = "${event.timestamp},$type,${event.values[0]},${event.values[1]},${event.values[2]},${event.accuracy}\n"

        try {
            imuWriter?.write(line)
            imuSampleCount++

            // Flush every 1000 samples to balance performance vs data safety
            if (imuSampleCount % 1000 == 0L) {
                imuWriter?.flush()
            }
        } catch (e: Exception) {
            Log.e(TAG, "IMU write error", e)
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    // --- GPS ---

    private fun startGPS() {
        val request = LocationRequest.Builder(Priority.PRIORITY_HIGH_ACCURACY, GPS_INTERVAL_MS)
            .setMinUpdateIntervalMillis(GPS_INTERVAL_MS)
            .build()

        locationCallback = object : LocationCallback() {
            override fun onLocationResult(result: LocationResult) {
                if (!isLogging) return

                for (location in result.locations) {
                    writeGPS(location)
                }
            }
        }

        try {
            fusedLocationClient.requestLocationUpdates(request, locationCallback!!, mainLooper)
            Log.i(TAG, "GPS registered at ${GPS_INTERVAL_MS}ms")
        } catch (e: SecurityException) {
            Log.e(TAG, "GPS permission denied", e)
        }
    }

    private fun writeGPS(location: Location) {
        // getElapsedRealtimeNanos() — same clock as SensorEvent.timestamp
        val elapsed = location.elapsedRealtimeNanos

        val sats = if (location.extras != null) {
            location.extras?.getInt("satellites", -1) ?: -1
        } else -1

        val line = "$elapsed,${location.latitude},${location.longitude},${location.altitude}," +
                "${location.accuracy},${location.speed},${location.bearing},$sats\n"

        try {
            gpsWriter?.write(line)
            gpsWriter?.flush()  // GPS is low rate, always flush
            gpsSampleCount++
        } catch (e: Exception) {
            Log.e(TAG, "GPS write error", e)
        }
    }

    // --- Notification ---

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Gojo Sensor Logging",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Active sensor logging notification"
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    override fun onDestroy() {
        stopLogging()
        super.onDestroy()
    }
}
