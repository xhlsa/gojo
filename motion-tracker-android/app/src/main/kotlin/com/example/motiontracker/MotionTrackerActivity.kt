package com.example.motiontracker

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import android.util.Log
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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        Log.i(tag, "Activity created")

        try {
            initializeViews()
            setupButtonListeners()
            startService()
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
            samplesText.text = "Samples: $counts"

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
}
