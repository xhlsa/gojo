package com.gojo.logger

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import com.gojo.core.SensorService
import com.gojo.core.SensorThread
import org.osmdroid.config.Configuration
import org.osmdroid.tileprovider.tilesource.TileSourceFactory
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.Marker
import org.osmdroid.views.overlay.Polyline

/**
 * Step 4 — Live map view (OSMDroid, no API key required).
 *
 * Binds to [SensorService] which owns the [SensorThread] and the EKF handle.
 * Every 200 ms the UI thread reads [SensorThread.cachedState] and:
 *   - Updates the HUD (lat, lon, speed, heading, cov_trace, stationary flag)
 *   - Appends the EKF position to the green polyline (capped at 2 000 points)
 *   - Appends the raw GPS position to the red polyline (deduplicated, capped)
 *   - Moves the blue marker to the current EKF position
 *   - Pans the camera to follow; zooms to 17 on the first fix only
 *
 * The tile layer is inverted via a single ColorMatrix so the map renders dark
 * (classic night-mode look). The green EKF track pops well against the dark base.
 */
class MainActivity : AppCompatActivity() {

    // ── Service binding ───────────────────────────────────────────────────────

    private var sensorThread: SensorThread? = null
    private var serviceBound = false

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            sensorThread = (binder as SensorService.LocalBinder).thread
            serviceBound = true
        }
        override fun onServiceDisconnected(name: ComponentName) {
            sensorThread = null
            serviceBound = false
        }
    }

    // ── Map ───────────────────────────────────────────────────────────────────

    private lateinit var mapView:     MapView
    private lateinit var ekfPolyline: Polyline
    private lateinit var gpsPolyline: Polyline
    private lateinit var ekfMarker:   Marker

    private val ekfPoints = ArrayList<GeoPoint>(2048)
    private val gpsPoints = ArrayList<GeoPoint>(2048)

    // Avoid appending a duplicate GPS point on every 200 ms tick.
    private var lastRawLat = Double.NaN
    private var lastRawLon = Double.NaN

    // ── HUD ───────────────────────────────────────────────────────────────────

    private lateinit var statusText: TextView

    private val uiHandler  = Handler(Looper.getMainLooper())
    private val uiRunnable = object : Runnable {
        override fun run() {
            updateUi()
            uiHandler.postDelayed(this, 200L)
        }
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        // Must be set before MapView is inflated (OSMDroid tile usage policy).
        Configuration.getInstance().userAgentValue = "Gojo/1.0"

        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        statusText      = findViewById(R.id.status_text)
        statusText.text = "Waiting for GPS…"

        // ── Map setup ──────────────────────────────────────────────────────
        mapView = findViewById(R.id.map_view)
        mapView.setTileSource(TileSourceFactory.MAPNIK)
        mapView.setMultiTouchControls(true)
        mapView.controller.setZoom(3.0) // start zoomed out until first fix

        // Invert tile colours → dark map. Single 4×5 ColorMatrix, no hacks.
        mapView.overlayManager.tilesOverlay.setColorFilter(
            ColorMatrixColorFilter(ColorMatrix(floatArrayOf(
                -1f,  0f,  0f, 0f, 255f,
                 0f, -1f,  0f, 0f, 255f,
                 0f,  0f, -1f, 0f, 255f,
                 0f,  0f,  0f, 1f,   0f,
            )))
        )

        // ── Overlays — added once and mutated in place ─────────────────────
        gpsPolyline = Polyline(mapView).apply {
            outlinePaint.color       = Color.RED
            outlinePaint.strokeWidth = 4f
        }
        ekfPolyline = Polyline(mapView).apply {
            outlinePaint.color       = Color.GREEN
            outlinePaint.strokeWidth = 6f
        }
        ekfMarker = Marker(mapView).apply {
            setAnchor(Marker.ANCHOR_CENTER, Marker.ANCHOR_BOTTOM)
            title = "EKF position"
        }
        // GPS polyline below EKF polyline; marker on top.
        mapView.overlays.addAll(listOf(gpsPolyline, ekfPolyline, ekfMarker))

        requestPermissionsIfNeeded()
    }

    override fun onStart() {
        super.onStart()
        val intent = Intent(this, SensorService::class.java)
        startForegroundService(intent)
        bindService(intent, connection, BIND_AUTO_CREATE)
        uiHandler.post(uiRunnable)
    }

    override fun onResume() {
        super.onResume()
        mapView.onResume()
    }

    override fun onPause() {
        mapView.onPause()
        super.onPause()
    }

    override fun onStop() {
        uiHandler.removeCallbacks(uiRunnable)
        if (serviceBound) {
            unbindService(connection)
            serviceBound = false
        }
        super.onStop()
    }

    override fun onDestroy() {
        mapView.onDetach()
        super.onDestroy()
    }

    // ── UI update (200 ms) ────────────────────────────────────────────────────

    private fun updateUi() {
        val thread = sensorThread ?: return
        val state  = thread.cachedState
        if (state.size < 12) return

        val lat   = state[0]; val lon   = state[1]
        val speed = state[3]; val hdg   = state[4]
        val cov   = state[7]; val stat  = state[10]

        statusText.text = buildString {
            appendLine("lat  %.6f   lon  %.6f".format(lat, lon))
            appendLine("spd  %.1f m/s    hdg  %.1f°".format(speed, hdg))
            append    ("cov  %.3f   stationary  %s".format(cov, if (stat > 0.5) "YES" else "NO"))
        }

        if (lat == 0.0 && lon == 0.0) return // EKF not yet initialised

        // ── EKF track (cap at 2 000 points) ───────────────────────────────
        val ekfPos = GeoPoint(lat, lon)
        if (ekfPoints.size >= 2000) ekfPoints.removeAt(0)
        ekfPoints.add(ekfPos)
        ekfPolyline.setPoints(ekfPoints)

        // ── Raw GPS track (deduplicated, cap at 2 000 points) ─────────────
        val rawLat = thread.rawGpsLat
        val rawLon = thread.rawGpsLon
        if (!rawLat.isNaN() && (rawLat != lastRawLat || rawLon != lastRawLon)) {
            if (gpsPoints.size >= 2000) gpsPoints.removeAt(0)
            gpsPoints.add(GeoPoint(rawLat, rawLon))
            gpsPolyline.setPoints(gpsPoints)
            lastRawLat = rawLat
            lastRawLon = rawLon
        }

        // ── Marker ────────────────────────────────────────────────────────
        ekfMarker.position = ekfPos

        // ── Camera follow — zoom to street level on first fix, pan after ──
        if (ekfPoints.size == 1) {
            mapView.controller.setZoom(17.0)
            mapView.controller.setCenter(ekfPos)
        } else {
            mapView.controller.animateTo(ekfPos)
        }

        mapView.invalidate()
    }

    // ── Permissions ───────────────────────────────────────────────────────────

    private fun requestPermissionsIfNeeded() {
        val needed = buildList {
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED
            ) add(Manifest.permission.ACCESS_FINE_LOCATION)

            if (android.os.Build.VERSION.SDK_INT >= 33 &&
                checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQ_CODE)
        }
    }

    companion object {
        private const val REQ_CODE = 42
    }
}
