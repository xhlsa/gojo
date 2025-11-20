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
import androidx.appcompat.app.AlertDialog
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.SavedStateViewModelFactory
import com.example.motiontracker.data.SessionConfig

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

    // ViewModel for session state management
    private lateinit var sessionViewModel: SessionViewModel

    companion object {
        private const val PERMISSION_REQUEST_CODE = 100
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        Log.i(tag, "Activity created")

        try {
            initializeViews()

            // Initialize ViewModel (persists across lifecycle events)
            sessionViewModel = ViewModelProvider(
                this,
                SavedStateViewModelFactory(application, this)
            ).get(SessionViewModel::class.java)

            setupButtonListeners()

            // Start service immediately (infrastructure only, no JNI recording)
            startService()

            // Bind ViewModel to service for LiveData updates
            bindViewModelToService()

            updateStatus()
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
            // Gate everything on the Start button
            if (!hasRequiredPermissions()) {
                Log.w(tag, "Requesting permissions...")
                requestRequiredPermissions()
                return@setOnClickListener
            }

            try {
                Log.d(tag, "Starting session...")

                // Create config with device info
                val config = SessionConfig.default()

                // Start JNI session
                JniBinding.startSessionWithConfig(config)
                Log.d(tag, "JNI session started")

                // Start session writer (persistence layer)
                val service = getMotionTrackerService()
                service?.startSessionWriter(config)
                Log.d(tag, "Session writer started")

                // Update ViewModel
                sessionViewModel.startRecording()
                sessionViewModel.setConfig(config)

                updateStatus()
                Log.i(tag, "✓ Session recording started")
            } catch (e: Exception) {
                statusText.text = "Error starting: ${e.message}"
                Log.e(tag, "Failed to start session", e)
            }
        }

        stopButton.setOnClickListener {
            try {
                Log.d(tag, "Stopping session...")

                // Finalize session writer before stopping JNI session
                val service = getMotionTrackerService()
                val finalPath = service?.finalizeSessionWriter()
                if (finalPath != null) {
                    Log.i(tag, "✓ Session saved to: $finalPath")
                    statusText.text = "Session saved"
                }

                // Stop JNI session
                JniBinding.stopSession()

                // Update ViewModel
                sessionViewModel.stopRecording()

                updateStatus()
                Log.i(tag, "✓ Session stopped")
            } catch (e: Exception) {
                statusText.text = "Error stopping: ${e.message}"
                Log.e(tag, "Failed to stop session", e)
            }
        }

        pauseButton.setOnClickListener {
            try {
                JniBinding.pauseSession()
                sessionViewModel.pauseRecording()
                updateStatus()
                Log.d(tag, "Session paused")
            } catch (e: Exception) {
                statusText.text = "Error pausing: ${e.message}"
                Log.e(tag, "Failed to pause session", e)
            }
        }

        resumeButton.setOnClickListener {
            try {
                JniBinding.resumeSession()
                sessionViewModel.resumeRecording()
                updateStatus()
                Log.d(tag, "Session resumed")
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
     * Check if all required permissions are granted
     * Required: LOCATION (fine + coarse) + BODY_SENSORS + FOREGROUND_SERVICE
     * Android 10+: Also requires ACCESS_BACKGROUND_LOCATION
     */
    private fun hasRequiredPermissions(): Boolean {
        val hasFineLocation = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        val hasCoarseLocation = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.ACCESS_COARSE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED

        val hasBodySensors = ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.BODY_SENSORS
        ) == PackageManager.PERMISSION_GRANTED

        val hasBackgroundLocation = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.ACCESS_BACKGROUND_LOCATION
            ) == PackageManager.PERMISSION_GRANTED
        } else {
            true  // Not required pre-Android 10
        }

        return hasFineLocation && hasCoarseLocation && hasBodySensors && hasBackgroundLocation
    }

    /**
     * Request required permissions from user
     * Gated on Start button tap
     */
    private fun requestRequiredPermissions() {
        Log.i(tag, "Requesting required permissions...")

        val permissions = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
            Manifest.permission.BODY_SENSORS
        )

        // Android 10+ requires background location
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            permissions.add(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
        }

        ActivityCompat.requestPermissions(
            this,
            permissions.toTypedArray(),
            PERMISSION_REQUEST_CODE
        )
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
                Log.i(tag, "✓ All permissions granted")
                statusText.text = "Permissions granted. Ready to record."
                startButton.isEnabled = true
            } else {
                Log.w(tag, "⚠ Permissions denied")
                showPermissionDeniedDialog()
                startButton.isEnabled = false
            }
        }
    }

    /**
     * Show persistent dialog explaining permission requirements
     */
    private fun showPermissionDeniedDialog() {
        AlertDialog.Builder(this)
            .setTitle("Permissions Required")
            .setMessage(
                "Motion Tracker requires the following permissions to work:\n\n" +
                "• Location (Fine & Coarse) - for GPS tracking\n" +
                "• Body Sensors - for accelerometer & gyroscope\n" +
                "• Foreground Service - to run tracking in background\n\n" +
                "Please grant these permissions in Settings to enable recording."
            )
            .setPositiveButton("Open Settings") { _, _ ->
                val intent = Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                intent.data = android.net.Uri.fromParts("package", packageName, null)
                startActivity(intent)
            }
            .setNegativeButton("Cancel") { _, _ ->
                statusText.text = "Permissions denied - cannot record"
            }
            .setCancelable(false)
            .show()
    }

    /**
     * Get reference to MotionTrackerService for direct calls
     * Uses static instance (simple pattern for foreground service)
     */
    private fun getMotionTrackerService(): MotionTrackerService? {
        return MotionTrackerService.getInstance()
    }

    /**
     * Bind ViewModel to service for LiveData updates
     */
    private fun bindViewModelToService() {
        try {
            // In a production app, use bound service with proper lifecycle handling
            // For now, the service gets ViewModel reference via callback
            // This will be populated when service connects to activity
            Log.d(tag, "ViewModel binding initialized")
        } catch (e: Exception) {
            Log.w(tag, "Failed to bind ViewModel to service", e)
        }
    }
}
