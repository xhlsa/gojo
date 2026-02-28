package com.gojo.core

import android.content.Intent
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Bundle
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.gojo.logger.MainActivity
import com.gojo.logger.R

/**
 * Stationary calibration screen shown before every session.
 *
 * Collects [TARGET_SAMPLES] accelerometer + gyroscope readings at ~50 Hz
 * (~3 seconds), then:
 *   - Checks accel variance; if the phone moved, resets and shows a warning.
 *   - Computes the mean gravity vector (gx, gy, gz) and gyro bias (bx, by, bz).
 *   - Stores the result in [GojoApp.instance.calibration].
 *   - Waits 1 second, then starts [MainActivity] and finishes.
 *
 * [SensorThread] reads [GojoApp.instance.calibration] in onLooperPrepared()
 * and applies it via [GojoJni.setCalibration] — no handle is shared between
 * this Activity and the Service.
 */
class CalibrationActivity : AppCompatActivity() {

    private lateinit var sensorManager: SensorManager
    private lateinit var statusView:    TextView

    private val accelSamples = ArrayList<Triple<Double, Double, Double>>(TARGET_SAMPLES + 8)
    private val gyroSamples  = ArrayList<Triple<Double, Double, Double>>(TARGET_SAMPLES + 8)

    private var calibrationDone = false

    private val sensorListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val v = event.values
            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER ->
                    if (accelSamples.size < TARGET_SAMPLES)
                        accelSamples.add(Triple(v[0].toDouble(), v[1].toDouble(), v[2].toDouble()))
                Sensor.TYPE_GYROSCOPE ->
                    if (gyroSamples.size < TARGET_SAMPLES)
                        gyroSamples.add(Triple(v[0].toDouble(), v[1].toDouble(), v[2].toDouble()))
            }
            if (!calibrationDone &&
                accelSamples.size >= TARGET_SAMPLES &&
                gyroSamples.size  >= TARGET_SAMPLES
            ) {
                finishCalibration()
            }
        }
        override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) {}
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_calibration)
        statusView    = findViewById(R.id.calibration_status)
        sensorManager = getSystemService(SENSOR_SERVICE) as SensorManager
    }

    override fun onResume() {
        super.onResume()
        startCollection()
    }

    override fun onPause() {
        sensorManager.unregisterListener(sensorListener)
        super.onPause()
    }

    // ── Collection ────────────────────────────────────────────────────────────

    private fun startCollection() {
        accelSamples.clear()
        gyroSamples.clear()
        calibrationDone = false
        statusView.text = "Hold phone still\ncalibrating…"

        sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)?.let { s ->
            sensorManager.registerListener(sensorListener, s, SensorManager.SENSOR_DELAY_GAME)
        }
        sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)?.let { s ->
            sensorManager.registerListener(sensorListener, s, SensorManager.SENSOR_DELAY_GAME)
        }
    }

    private fun finishCalibration() {
        calibrationDone = true
        sensorManager.unregisterListener(sensorListener)

        // Movement check — sum of per-axis variance across the accel window.
        val ax = accelSamples.map { it.first }
        val ay = accelSamples.map { it.second }
        val az = accelSamples.map { it.third }
        val totalVariance = varianceOf(ax) + varianceOf(ay) + varianceOf(az)

        if (totalVariance > VARIANCE_THRESHOLD) {
            // Phone moved — restart collection after a brief warning.
            runOnUiThread {
                statusView.text = "Too much movement\nhold still…"
                window.decorView.postDelayed({ startCollection() }, 1_500L)
            }
            return
        }

        // Compute mean gravity vector and gyro bias.
        val gx = ax.average(); val gy = ay.average(); val gz = az.average()
        val bx = gyroSamples.map { it.first  }.average()
        val by = gyroSamples.map { it.second }.average()
        val bz = gyroSamples.map { it.third  }.average()

        GojoApp.instance.calibration = GojoApp.CalibrationData(gx, gy, gz, bx, by, bz)

        runOnUiThread {
            statusView.text = "Calibration complete"
            window.decorView.postDelayed({
                startActivity(Intent(this, MainActivity::class.java))
                finish()
            }, 1_000L)
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun varianceOf(values: List<Double>): Double {
        val mean = values.average()
        return values.sumOf { (it - mean) * (it - mean) } / values.size
    }

    companion object {
        private const val TARGET_SAMPLES     = 150
        private const val VARIANCE_THRESHOLD = 1.0   // m²/s⁴ — ~0.1g RMS motion
    }
}
