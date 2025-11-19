package com.example.motiontracker

import android.content.Context
import android.location.Criteria
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.util.Log

/**
 * Real-time GPS location collection via LocationManager
 *
 * Requests location updates from Android LocationManager:
 * - Hybrid: Network provider (fast, low accuracy) + GPS (slow, high accuracy)
 * - Fallback: Network only if GPS unavailable
 * - Graceful: Service continues if location unavailable
 *
 * Pushes GpsSample to Rust JNI core for EKF sensor fusion
 */
class LocationCollector(
    private val context: Context,
    private val locationManager: LocationManager
) : LocationListener {
    private val tag = "MotionTracker.Location"

    private var gpsCount = 0
    private var networkCount = 0
    private var lastLocation: Location? = null
    private var lastLocationTime = 0L

    // Thresholds for logging
    companion object {
        private const val MIN_ACCURACY = 5.0  // meters (ignore > 5m error)
        private const val MAX_ACCURACY = 100.0  // meters (warn > 100m)
        private const val MIN_SPEED = 0.2  // m/s (< 0.72 km/h is "stationary")
    }

    /**
     * Request location updates
     * Tries GPS first, falls back to network if unavailable
     *
     * @throws Exception if location providers unavailable
     */
    @Throws(Exception::class)
    fun start() {
        try {
            // Build criteria: high accuracy, uses GPS
            val criteria = Criteria().apply {
                accuracy = Criteria.ACCURACY_FINE
                isAltitudeRequired = true
                isBearingRequired = true
                isSpeedRequired = true
                powerRequirement = Criteria.POWER_HIGH
            }

            // Try GPS provider first
            val gpsProvider = locationManager.getBestProvider(criteria, false)
                ?: throw Exception("No location provider available")

            Log.i(tag, "Starting location collection with provider: $gpsProvider")

            // Request updates: 5 second minimum interval, 0 meter minimum distance
            locationManager.requestLocationUpdates(
                gpsProvider,
                5000L,  // 5 second minimum interval
                0f,     // 0 meter minimum distance
                this
            )

            // Try to get initial location (might be cached)
            val lastLocation = locationManager.getLastKnownLocation(gpsProvider)
            if (lastLocation != null) {
                pushLocation(lastLocation)
                Log.d(tag, "Got initial location: ${lastLocation.latitude}, ${lastLocation.longitude}")
            } else {
                Log.d(tag, "No initial location (first fix pending)")
            }

            gpsCount = 0
            networkCount = 0

            Log.i(tag, "✓ Location collection started")
        } catch (e: Exception) {
            Log.e(tag, "Failed to start location collection", e)
            throw e
        }
    }

    /**
     * Stop location updates
     */
    fun stop() {
        try {
            locationManager.removeUpdates(this)
            Log.i(tag, "✓ Location collection stopped")
            Log.d(
                tag,
                "  GPS fixes: $gpsCount, Network: $networkCount"
            )
        } catch (e: Exception) {
            Log.e(tag, "Error stopping location collection", e)
        }
    }

    /**
     * LocationListener callback on location change
     */
    override fun onLocationChanged(location: Location) {
        try {
            pushLocation(location)

            when (location.provider) {
                LocationManager.GPS_PROVIDER -> gpsCount++
                LocationManager.NETWORK_PROVIDER -> networkCount++
                else -> {} // Other provider
            }

            lastLocation = location

            // Log every 10th fix or on accuracy change
            if ((gpsCount + networkCount) % 10 == 0) {
                val accuracy = String.format("%.1f", location.accuracy)
                val speed = String.format("%.1f", location.speed * 3.6)  // Convert m/s to km/h
                Log.d(
                    tag,
                    "Fix #${gpsCount + networkCount}: ${location.provider} @ $accuracy m, ${speed} km/h"
                )
            }

            // Accuracy anomalies
            if (location.accuracy > MAX_ACCURACY) {
                Log.w(
                    tag,
                    "Low accuracy: ${String.format("%.1f", location.accuracy)}m (lat=${location.latitude}, lon=${location.longitude})"
                )
            }

            // Rate monitoring
            val now = System.currentTimeMillis()
            if (lastLocationTime > 0) {
                val dt = (now - lastLocationTime) / 1000.0
                if (dt > 30) {  // Log if gap > 30s
                    Log.d(tag, "Location gap: ${dt}s")
                }
            }
            lastLocationTime = now
        } catch (e: Exception) {
            Log.e(tag, "Error processing location", e)
        }
    }

    /**
     * LocationListener callback on provider enabled
     */
    override fun onProviderEnabled(provider: String) {
        Log.d(tag, "Location provider enabled: $provider")
    }

    /**
     * LocationListener callback on provider disabled
     */
    override fun onProviderDisabled(provider: String) {
        Log.d(tag, "Location provider disabled: $provider")
    }

    /**
     * LocationListener callback on status change (pre-Marshmallow)
     */
    override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {
        val statusStr = when (status) {
            android.location.LocationProvider.OUT_OF_SERVICE -> "OUT_OF_SERVICE"
            android.location.LocationProvider.TEMPORARILY_UNAVAILABLE -> "TEMPORARILY_UNAVAILABLE"
            android.location.LocationProvider.AVAILABLE -> "AVAILABLE"
            else -> "UNKNOWN"
        }
        Log.d(tag, "Provider $provider status changed: $statusStr")
    }

    /**
     * Push location to Rust JNI core
     * Converts Android Location to GpsSample format
     */
    private fun pushLocation(location: Location) {
        try {
            val timestamp = System.currentTimeMillis() / 1000.0
            JniBinding.pushGpsSample(
                latitude = location.latitude,
                longitude = location.longitude,
                altitude = location.altitude,
                accuracy = location.accuracy.toDouble(),
                speed = location.speed.toDouble(),  // Already in m/s
                bearing = location.bearing.toDouble(),
                timestamp = timestamp
            )
        } catch (e: Exception) {
            Log.e(tag, "Failed to push GPS sample", e)
        }
    }

    /**
     * Get fix counts for monitoring
     */
    fun getFixCounts(): Pair<Int, Int> {
        return Pair(gpsCount, networkCount)
    }

    /**
     * Get last location (for status display)
     */
    fun getLastLocation(): Location? {
        return lastLocation
    }

    /**
     * Check if we have at least one fix
     */
    fun hasLocation(): Boolean {
        return lastLocation != null
    }
}

/**
 * Wrapper for LocationManager with error handling
 */
class AndroidLocationManager(
    private val context: Context,
    private val locationManager: LocationManager
) {
    private val tag = "MotionTracker.AndroidLM"
    private var collector: LocationCollector? = null

    fun startCollection() {
        try {
            collector = LocationCollector(context, locationManager)
            collector?.start()
        } catch (e: Exception) {
            Log.e(tag, "Failed to start location collection", e)
            // Don't throw - location is optional (allow inertial-only tracking)
            Log.i(tag, "Location disabled - continuing with inertial tracking")
        }
    }

    fun stopCollection() {
        collector?.stop()
        collector = null
    }

    fun isCollecting(): Boolean {
        return collector != null
    }

    fun getFixCounts(): Pair<Int, Int> {
        return collector?.getFixCounts() ?: Pair(0, 0)
    }

    fun hasLocation(): Boolean {
        return collector?.hasLocation() ?: false
    }

    fun getLastLocation(): Location? {
        return collector?.getLastLocation()
    }
}
