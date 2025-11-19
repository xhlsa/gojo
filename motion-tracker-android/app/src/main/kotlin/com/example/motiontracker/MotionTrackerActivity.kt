package com.example.motiontracker

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import android.util.Log
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * Motion Tracker Main Activity
 *
 * UI for:
 * - Starting/stopping recording
 * - Pausing/resuming session
 * - Viewing real-time sample counts
 * - Displaying session status
 */
class MotionTrackerActivity : AppCompatActivity() {
    private val tag = "MotionTracker.Activity"

    private lateinit var statusText: TextView
    private lateinit var samplesText: TextView
    private lateinit var startButton: Button
    private lateinit var stopButton: Button
    private lateinit var pauseButton: Button
    private lateinit var resumeButton: Button

    companion object {
        private const val PERMISSION_REQUEST_CODE = 100
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        Log.i(tag, "Activity created")

        try {
            initializeViews()
            setupButtonListeners()

            // Request location permissions (Android 6+)
            if (!hasLocationPermissions()) {
                requestLocationPermissions()
            } else {
                startService()
                updateStatus()
            }
        } catch (e: Exception) {
            Log.e(tag, "Failed to initialize activity", e)
            statusText.text = "Error: ${e.message}"
        }
    }

    override fun onResume() {
        super.onResume()
        // Refresh status periodically
        updateStatus()
    }

    /**
     * Initialize UI views
     */
    private fun initializeViews() {
        statusText = findViewById(R.id.status_text)
        samplesText = findViewById(R.id.samples_text)
        startButton = findViewById(R.id.start_button)
        stopButton = findViewById(R.id.stop_button)
        pauseButton = findViewById(R.id.pause_button)
        resumeButton = findViewById(R.id.resume_button)
    }

    /**
     * Setup button click listeners
     */
    private fun setupButtonListeners() {
        startButton.setOnClickListener {
            try {
                JniBinding.startSession()
                updateStatus()
            } catch (e: Exception) {
                statusText.text = "Error starting: ${e.message}"
                Log.e(tag, "Failed to start session", e)
            }
        }

        stopButton.setOnClickListener {
            try {
                JniBinding.stopSession()
                updateStatus()
            } catch (e: Exception) {
                statusText.text = "Error stopping: ${e.message}"
                Log.e(tag, "Failed to stop session", e)
            }
        }

        pauseButton.setOnClickListener {
            try {
                JniBinding.pauseSession()
                updateStatus()
            } catch (e: Exception) {
                statusText.text = "Error pausing: ${e.message}"
                Log.e(tag, "Failed to pause session", e)
            }
        }

        resumeButton.setOnClickListener {
            try {
                JniBinding.resumeSession()
                updateStatus()
            } catch (e: Exception) {
                statusText.text = "Error resuming: ${e.message}"
                Log.e(tag, "Failed to resume session", e)
            }
        }
    }

    /**
     * Start background service if not already running
     */
    private fun startService() {
        val serviceIntent = Intent(this, MotionTrackerService::class.java)
        ContextCompat.startForegroundService(this, serviceIntent)
        Log.d(tag, "Service started")
    }

    /**
     * Update status display
     */
    private fun updateStatus() {
        try {
            val state = JniBinding.getSessionState()
            val counts = JniBinding.getSampleCountsLabeled()

            statusText.text = "State: ${state.name}"
            samplesText.text = "Accel: ${counts.accel} | Gyro: ${counts.gyro} | GPS: ${counts.gps}"

            // Enable/disable buttons based on state
            when (state) {
                SessionState.IDLE -> {
                    startButton.isEnabled = true
                    stopButton.isEnabled = false
                    pauseButton.isEnabled = false
                    resumeButton.isEnabled = false
                }
                SessionState.RECORDING -> {
                    startButton.isEnabled = false
                    stopButton.isEnabled = true
                    pauseButton.isEnabled = true
                    resumeButton.isEnabled = false
                }
                SessionState.PAUSED -> {
                    startButton.isEnabled = false
                    stopButton.isEnabled = true
                    pauseButton.isEnabled = false
                    resumeButton.isEnabled = true
                }
            }

            Log.d(tag, "Status updated: $state, $counts")
        } catch (e: Exception) {
            statusText.text = "Error: ${e.message}"
            Log.e(tag, "Failed to update status", e)
        }
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

    /**
     * Request location permissions from user (Android 6+)
     */
    private fun requestLocationPermissions() {
        Log.i(tag, "Requesting location permissions...")

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION
                ),
                PERMISSION_REQUEST_CODE
            )
        }
    }

    /**
     * Handle permission request result
     */
    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)

        if (requestCode == PERMISSION_REQUEST_CODE) {
            val allGranted = grantResults.all { it == PackageManager.PERMISSION_GRANTED }

            if (allGranted) {
                Log.i(tag, "✓ Location permissions granted")
                statusText.text = "Permissions granted, starting service..."
                startService()
                updateStatus()
            } else {
                Log.w(tag, "⚠ Location permissions denied")
                statusText.text = "Location permissions required for GPS tracking"
                // Service will still start but GPS will fail gracefully
                startService()
                updateStatus()
            }
        }
    }
}
