package com.example.motiontracker

import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.util.Log
import kotlin.math.abs

/**
 * Real-time sensor collection from accelerometer and gyroscope
 *
 * Uses Android SensorManager to receive callbacks from:
 * - TYPE_ACCELEROMETER (m/s²)
 * - TYPE_GYROSCOPE (rad/s)
 *
 * Converts sensor events to JNI format and pushes to Rust in real-time
 * Handles sensor startup, accuracy changes, and graceful shutdown
 */
class SensorCollector(private val sensorManager: SensorManager) : SensorEventListener {
    private val tag = "MotionTracker.Sensors"

    private var accelSensor: Sensor? = null
    private var gyroSensor: Sensor? = null

    private var accelCount = 0
    private var gyroCount = 0

    // Sensor accuracy state
    private var accelAccuracy = SensorManager.SENSOR_STATUS_ACCURACY_LOW
    private var gyroAccuracy = SensorManager.SENSOR_STATUS_ACCURACY_LOW

    // Last sample timestamps for rate monitoring
    private var lastAccelTime = 0L
    private var lastGyroTime = 0L

    // Running averages for accuracy logging
    private var accelMagnitudeSum = 0.0
    private var gyroMagnitudeSum = 0.0

    companion object {
        // Sensor rates: delay in microseconds
        private const val ACCEL_DELAY_US = 20_000  // ~50 Hz
        private const val GYRO_DELAY_US = 20_000   // ~50 Hz

        // Thresholds for logging anomalies
        private const val MIN_ACCEL_MAGNITUDE = 1.0  // Should be ~9.8 at rest
        private const val MAX_ACCEL_MAGNITUDE = 20.0
        private const val MAX_GYRO_MAGNITUDE = 5.0  // rad/s (extreme rotation)
    }

    /**
     * Start sensor collection
     * Registers callbacks for accel and gyro
     *
     * @throws Exception if sensors unavailable
     */
    @Throws(Exception::class)
    fun start() {
        try {
            accelSensor = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
                ?: throw Exception("Accelerometer not available")
            gyroSensor = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
                ?: throw Exception("Gyroscope not available")

            val accelRegistered = sensorManager.registerListener(
                this,
                accelSensor,
                ACCEL_DELAY_US
            )
            val gyroRegistered = sensorManager.registerListener(
                this,
                gyroSensor,
                GYRO_DELAY_US
            )

            if (!accelRegistered || !gyroRegistered) {
                throw Exception("Failed to register sensor listeners")
            }

            accelCount = 0
            gyroCount = 0

            Log.i(tag, "✓ Sensor collection started")
            Log.d(tag, "  Accel: ${accelSensor?.name} @ ${accelSensor?.power}mA")
            Log.d(tag, "  Gyro: ${gyroSensor?.name} @ ${gyroSensor?.power}mA")
        } catch (e: Exception) {
            Log.e(tag, "Failed to start sensor collection", e)
            throw e
        }
    }

    /**
     * Stop sensor collection
     * Unregisters all listeners
     */
    fun stop() {
        try {
            sensorManager.unregisterListener(this)
            Log.i(tag, "✓ Sensor collection stopped")
            Log.d(
                tag,
                "  Samples: Accel=$accelCount, Gyro=$gyroCount"
            )
        } catch (e: Exception) {
            Log.e(tag, "Error stopping sensor collection", e)
        }
    }

    /**
     * SensorEventListener callback for sensor events
     * Called on sensor data changes
     */
    override fun onSensorChanged(event: SensorEvent) {
        try {
            val timestamp = event.timestamp / 1_000_000_000.0  // Convert to seconds

            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> {
                    val x = event.values[0].toDouble()
                    val y = event.values[1].toDouble()
                    val z = event.values[2].toDouble()

                    // Push to Rust JNI
                    JniBinding.pushAccelSample(x, y, z, timestamp)

                    accelCount++
                    val magnitude = kotlin.math.sqrt(x * x + y * y + z * z)
                    accelMagnitudeSum += magnitude

                    // Log anomalies
                    if (magnitude < MIN_ACCEL_MAGNITUDE || magnitude > MAX_ACCEL_MAGNITUDE) {
                        Log.w(
                            tag,
                            "Accel anomaly: mag=$magnitude (x=$x, y=$y, z=$z)"
                        )
                    }

                    // Rate monitoring
                    val now = System.currentTimeMillis()
                    if (lastAccelTime > 0) {
                        val dt = (now - lastAccelTime) / 1000.0
                        if (dt > 0.1) {  // Log if gap > 100ms
                            Log.d(tag, "Accel gap: ${dt * 1000}ms")
                        }
                    }
                    lastAccelTime = now

                    if (accelCount % 100 == 0) {
                        Log.d(
                            tag,
                            "Accel: $accelCount samples, avg_mag=${accelMagnitudeSum / accelCount}"
                        )
                    }
                }

                Sensor.TYPE_GYROSCOPE -> {
                    val x = event.values[0].toDouble()
                    val y = event.values[1].toDouble()
                    val z = event.values[2].toDouble()

                    // Push to Rust JNI
                    JniBinding.pushGyroSample(x, y, z, timestamp)

                    gyroCount++
                    val magnitude = kotlin.math.sqrt(x * x + y * y + z * z)
                    gyroMagnitudeSum += magnitude

                    // Log anomalies
                    if (magnitude > MAX_GYRO_MAGNITUDE) {
                        Log.w(
                            tag,
                            "Gyro anomaly: mag=$magnitude (x=$x, y=$y, z=$z)"
                        )
                    }

                    // Rate monitoring
                    val now = System.currentTimeMillis()
                    if (lastGyroTime > 0) {
                        val dt = (now - lastGyroTime) / 1000.0
                        if (dt > 0.1) {  // Log if gap > 100ms
                            Log.d(tag, "Gyro gap: ${dt * 1000}ms")
                        }
                    }
                    lastGyroTime = now

                    if (gyroCount % 100 == 0) {
                        Log.d(
                            tag,
                            "Gyro: $gyroCount samples, avg_mag=${gyroMagnitudeSum / gyroCount}"
                        )
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(tag, "Error processing sensor event", e)
        }
    }

    /**
     * SensorEventListener callback for accuracy changes
     */
    override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) {
        val accuracyStr = when (accuracy) {
            SensorManager.SENSOR_STATUS_UNRELIABLE -> "UNRELIABLE"
            SensorManager.SENSOR_STATUS_ACCURACY_LOW -> "LOW"
            SensorManager.SENSOR_STATUS_ACCURACY_MEDIUM -> "MEDIUM"
            SensorManager.SENSOR_STATUS_ACCURACY_HIGH -> "HIGH"
            else -> "UNKNOWN"
        }

        when (sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> {
                accelAccuracy = accuracy
                Log.d(tag, "Accel accuracy: $accuracyStr")
            }
            Sensor.TYPE_GYROSCOPE -> {
                gyroAccuracy = accuracy
                Log.d(tag, "Gyro accuracy: $accuracyStr")
            }
        }
    }

    /**
     * Get sample counts for monitoring
     */
    fun getSampleCounts(): Pair<Int, Int> {
        return Pair(accelCount, gyroCount)
    }

    /**
     * Get average magnitudes for diagnostics
     */
    fun getAverageMagnitudes(): Pair<Double, Double> {
        val avgAccel = if (accelCount > 0) accelMagnitudeSum / accelCount else 0.0
        val avgGyro = if (gyroCount > 0) gyroMagnitudeSum / gyroCount else 0.0
        return Pair(avgAccel, avgGyro)
    }
}

/**
 * Wrapper for Android SensorManager with error handling
 */
class AndroidSensorManager(private val sensorManager: SensorManager) {
    private val tag = "MotionTracker.AndroidSM"
    private var collector: SensorCollector? = null

    fun startCollection() {
        try {
            collector = SensorCollector(sensorManager)
            collector?.start()
        } catch (e: Exception) {
            Log.e(tag, "Failed to start sensor collection", e)
            throw MotionTrackerException("Sensor collection failed: ${e.message}", e)
        }
    }

    fun stopCollection() {
        collector?.stop()
        collector = null
    }

    fun isCollecting(): Boolean {
        return collector != null
    }

    fun getSampleCounts(): Pair<Int, Int> {
        return collector?.getSampleCounts() ?: Pair(0, 0)
    }
}
