package com.gojo.core

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
import android.location.LocationListener
import android.location.LocationManager
import android.os.Binder
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * Foreground service that owns sensor registration.
 *
 * It starts [SensorThread], registers IMU and GPS callbacks on that thread's
 * looper (so callbacks arrive directly on the filter thread — no extra posting
 * required), and exposes a [LocalBinder] so [MainActivity] can read
 * [SensorThread.cachedState] without going through an IPC.
 *
 * Start this service with startForegroundService() before binding so it
 * survives Activity rotation.
 */
class SensorService : Service() {

    // ── Binder ────────────────────────────────────────────────────────────────

    inner class LocalBinder : Binder() {
        val thread: SensorThread get() = sensorThread
    }

    private val binder = LocalBinder()

    // ── Internals ─────────────────────────────────────────────────────────────

    private lateinit var sensorThread:   SensorThread
    private lateinit var sensorManager:  SensorManager
    private lateinit var locationManager: LocationManager

    // Callbacks are registered with sensorThread.looper → called on that thread.

    private val imuListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val v  = event.values
            val ts = event.timestamp
            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER ->
                    sensorThread.onAccel(v[0].toDouble(), v[1].toDouble(), v[2].toDouble(), ts)
                Sensor.TYPE_GYROSCOPE ->
                    sensorThread.onGyro(v[0].toDouble(), v[1].toDouble(), v[2].toDouble())
            }
        }
        override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) {}
    }

    private val gpsListener = LocationListener { loc: Location ->
        sensorThread.onGps(
            lat         = loc.latitude,
            lon         = loc.longitude,
            alt         = loc.altitude,
            accuracy    = loc.accuracy,
            speed       = loc.speed,
            bearing     = loc.bearing,
            timestampNs = loc.elapsedRealtimeNanos,
        )
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate() {
        super.onCreate()

        sensorThread = SensorThread()
        sensorThread.start()
        // getLooper() blocks until onLooperPrepared() has run → handler and
        // ekfHandle are guaranteed to be initialised by the time we return.
        sensorThread.looper

        sensorManager   = getSystemService(SENSOR_SERVICE)   as SensorManager
        locationManager = getSystemService(LOCATION_SERVICE) as LocationManager

        startForeground(NOTIF_ID, buildNotification())
        registerSensors()
    }

    override fun onBind(intent: Intent): IBinder = binder

    override fun onDestroy() {
        unregisterSensors()
        sensorThread.shutdown()
        super.onDestroy()
    }

    // ── Sensor registration ───────────────────────────────────────────────────

    private fun registerSensors() {
        val looper = sensorThread.looper // deliver callbacks on the filter thread

        sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)?.let { sensor ->
            sensorManager.registerListener(imuListener, sensor, SensorManager.SENSOR_DELAY_FASTEST, looper)
        }
        sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)?.let { sensor ->
            sensorManager.registerListener(imuListener, sensor, SensorManager.SENSOR_DELAY_FASTEST, looper)
        }

        try {
            locationManager.requestLocationUpdates(
                LocationManager.GPS_PROVIDER,
                1_000L, // minimum interval (ms)
                0f,     // minimum distance (m)
                gpsListener,
                sensorThread.looper,
            )
        } catch (_: SecurityException) {
            // Location permission not yet granted — GPS will start once user accepts.
        }
    }

    private fun unregisterSensors() {
        sensorManager.unregisterListener(imuListener)
        try { locationManager.removeUpdates(gpsListener) } catch (_: Exception) {}
    }

    // ── Foreground notification ───────────────────────────────────────────────

    private fun buildNotification(): Notification {
        val channelId = "gojo_sensor"
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val ch = NotificationChannel(
                channelId,
                "Gojo Sensors",
                NotificationManager.IMPORTANCE_LOW,
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
        }
        return NotificationCompat.Builder(this, channelId)
            .setContentTitle("Gojo")
            .setContentText("Sensor fusion running")
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .build()
    }

    companion object {
        private const val NOTIF_ID = 1
    }
}
