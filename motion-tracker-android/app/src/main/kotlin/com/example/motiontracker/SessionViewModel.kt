package com.example.motiontracker

import android.app.Application
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.SavedStateHandle
import com.example.motiontracker.data.GpsStatus
import com.example.motiontracker.data.HealthAlert
import com.example.motiontracker.data.SessionConfig

/**
 * SessionViewModel - Centralized state for motion tracking session
 *
 * Manages:
 * - User-visible state (Idle/Recording/Paused)
 * - Elapsed time tracking
 * - GPS status from location collector
 * - Health alerts from monitor
 * - Session metadata (start time, sample counts)
 *
 * Persists state via SavedStateHandle for lifecycle awareness
 * Observed by MotionTrackerActivity and MotionTrackerService
 */
sealed class SessionUIState {
    object Idle : SessionUIState()
    data class Recording(val elapsedSeconds: Long = 0L) : SessionUIState()
    object Paused : SessionUIState()
    data class Error(val message: String) : SessionUIState()
}

class SessionViewModel(
    application: Application,
    private val savedState: SavedStateHandle
) : AndroidViewModel(application) {
    private val tag = "MotionTracker.ViewModel"

    // User-visible state (persisted across lifecycle events)
    private val _sessionState = MutableLiveData<SessionUIState>(SessionUIState.Idle)
    val sessionState: LiveData<SessionUIState> = _sessionState

    // Elapsed time in seconds (updated every ~1s by service)
    private val _elapsedSeconds = MutableLiveData<Long>(0L)
    val elapsedSeconds: LiveData<Long> = _elapsedSeconds

    // GPS status updates
    private val _gpsStatus = MutableLiveData<GpsStatus>(GpsStatus())
    val gpsStatus: LiveData<GpsStatus> = _gpsStatus

    // Health monitoring alerts
    private val _healthAlert = MutableLiveData<HealthAlert?>(null)
    val healthAlert: LiveData<HealthAlert?> = _healthAlert

    // Sample counts for UI display
    private val _accelCount = MutableLiveData<Int>(0)
    val accelCount: LiveData<Int> = _accelCount

    private val _gyroCount = MutableLiveData<Int>(0)
    val gyroCount: LiveData<Int> = _gyroCount

    private val _gpsCount = MutableLiveData<Int>(0)
    val gpsCount: LiveData<Int> = _gpsCount

    // Memory usage for diagnostics
    private val _memoryMb = MutableLiveData<Long>(0L)
    val memoryMb: LiveData<Long> = _memoryMb

    // Session config
    private val _config = MutableLiveData<SessionConfig>(SessionConfig.default())
    val config: LiveData<SessionConfig> = _config

    init {
        Log.d(tag, "SessionViewModel initialized")
    }

    /**
     * Transition to Recording state
     */
    fun startRecording() {
        _sessionState.value = SessionUIState.Recording(0L)
        _elapsedSeconds.value = 0L
        _healthAlert.value = null
        Log.d(tag, "Session started")
    }

    /**
     * Transition to Paused state
     */
    fun pauseRecording() {
        val current = _sessionState.value
        if (current is SessionUIState.Recording) {
            _sessionState.value = SessionUIState.Paused
            Log.d(tag, "Session paused at ${current.elapsedSeconds}s")
        }
    }

    /**
     * Resume from paused state
     */
    fun resumeRecording() {
        if (_sessionState.value is SessionUIState.Paused) {
            val elapsed = _elapsedSeconds.value ?: 0L
            _sessionState.value = SessionUIState.Recording(elapsed)
            Log.d(tag, "Session resumed from ${elapsed}s")
        }
    }

    /**
     * Transition to Idle state and reset
     */
    fun stopRecording() {
        _sessionState.value = SessionUIState.Idle
        _elapsedSeconds.value = 0L
        _healthAlert.value = null
        Log.d(tag, "Session stopped")
    }

    /**
     * Update elapsed time (called by service ticker every ~1s)
     */
    fun updateElapsedTime(seconds: Long) {
        _elapsedSeconds.value = seconds
        // Update Recording state with new elapsed time
        if (_sessionState.value is SessionUIState.Recording) {
            _sessionState.value = SessionUIState.Recording(seconds)
        }
    }

    /**
     * Update GPS status (called by location collector on fix)
     */
    fun updateGpsStatus(status: GpsStatus) {
        _gpsStatus.value = status
    }

    /**
     * Publish health alert (called by health monitor on sensor event)
     */
    fun publishHealthAlert(alert: HealthAlert) {
        _healthAlert.value = alert
        Log.w(tag, "Health alert: $alert")
    }

    /**
     * Clear current health alert (called by service on recovery)
     */
    fun clearHealthAlert() {
        _healthAlert.value = null
    }

    /**
     * Update sample counts (called by service)
     */
    fun updateSampleCounts(accel: Int, gyro: Int, gps: Int) {
        _accelCount.value = accel
        _gyroCount.value = gyro
        _gpsCount.value = gps
    }

    /**
     * Update memory usage (called by service for diagnostics)
     */
    fun updateMemoryUsage(mb: Long) {
        _memoryMb.value = mb
    }

    /**
     * Set session config before starting
     */
    fun setConfig(config: SessionConfig) {
        _config.value = config
    }

    /**
     * Get formatted elapsed time string
     * Example: "1m 23s"
     */
    fun getFormattedElapsedTime(): String {
        val seconds = _elapsedSeconds.value ?: 0L
        val minutes = seconds / 60
        val secs = seconds % 60
        return String.format("%dm %02ds", minutes, secs)
    }

    /**
     * Check if currently recording
     */
    fun isRecording(): Boolean {
        return _sessionState.value is SessionUIState.Recording
    }

    /**
     * Get current state
     */
    fun getState(): SessionUIState {
        return _sessionState.value ?: SessionUIState.Idle
    }

    override fun onCleared() {
        super.onCleared()
        Log.d(tag, "SessionViewModel cleared")
    }
}
