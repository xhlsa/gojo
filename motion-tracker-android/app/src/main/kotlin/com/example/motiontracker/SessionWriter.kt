package com.example.motiontracker

import android.content.Context
import android.util.Log
import com.example.motiontracker.data.SessionConfig
import com.example.motiontracker.storage.SessionDirectory
import com.example.motiontracker.storage.SessionStorage
import com.google.gson.Gson
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * SessionWriter - Periodic session data persistence
 *
 * Responsibilities:
 * - Stream chunks every ~15s (mirroring Termux behavior)
 * - Call Rust JNI to export current session state
 * - Write chunks to app-specific storage
 * - Write final.json + metadata.json on session end
 * - Handle graceful shutdown without data loss
 *
 * Usage:
 * ```
 * val writer = SessionWriter(context, config)
 * writer.start()  // Begin periodic chunk writing
 * // ... recording happens ...
 * writer.finalize()  // Write final export
 * ```
 */
class SessionWriter(
    private val context: Context,
    private val config: SessionConfig
) {
    private val tag = "MotionTracker.Writer"
    private val storage = SessionStorage(context)
    private var sessionDir: SessionDirectory? = null
    private var chunkIndex = 0
    private val running = AtomicBoolean(false)
    private var writerThread: Thread? = null

    // Chunk interval: 15 seconds
    companion object {
        private const val CHUNK_INTERVAL_MS = 15_000L
        private const val MIN_DISK_SPACE_MB = 50L  // Keep 50 MB free
    }

    /**
     * Initialize session directory and start periodic writer
     */
    fun start() {
        try {
            // Create session directory
            sessionDir = storage.createSessionDirectory(config)
            val dir = sessionDir ?: throw RuntimeException("Failed to create session directory")

            // Write metadata
            storage.writeMetadata(dir, config, System.currentTimeMillis())

            // Start writer thread
            running.set(true)
            writerThread = thread(name = "SessionWriter", daemon = false) {
                writerLoop()
            }

            Log.i(tag, "✓ Session writer started: ${dir.sessionName}")
        } catch (e: Exception) {
            Log.e(tag, "Failed to start session writer", e)
            throw e
        }
    }

    /**
     * Periodic writer loop: exports chunks every ~15s
     */
    private fun writerLoop() {
        while (running.get()) {
            try {
                // Check available disk space
                val availableMb = storage.getAvailableSpace() / (1024 * 1024)
                if (availableMb < MIN_DISK_SPACE_MB) {
                    Log.w(tag, "⚠ Low disk space: ${availableMb}MB available (need >${MIN_DISK_SPACE_MB}MB)")
                    // Continue anyway - chunks are important
                }

                // Export current session state from Rust
                val sessionJson = try {
                    JniBinding.getSessionJson()
                } catch (e: Exception) {
                    Log.e(tag, "Failed to export session JSON", e)
                    // Don't stop on export failure - try again next cycle
                    Thread.sleep(CHUNK_INTERVAL_MS)
                    continue
                }

                // Write chunk
                val dir = sessionDir
                if (dir != null) {
                    try {
                        storage.writeChunk(dir, chunkIndex, sessionJson)
                        chunkIndex++
                        Log.d(tag, "✓ Chunk $chunkIndex written (${dir.getChunkCount()} total)")
                    } catch (e: Exception) {
                        Log.e(tag, "Failed to write chunk", e)
                        // Don't stop writer on chunk failure
                    }
                }

                // Sleep until next chunk
                Thread.sleep(CHUNK_INTERVAL_MS)
            } catch (e: InterruptedException) {
                Log.d(tag, "Writer thread interrupted")
                break
            } catch (e: Exception) {
                Log.e(tag, "Error in writer loop", e)
                // Don't stop on errors - continue trying
                try {
                    Thread.sleep(CHUNK_INTERVAL_MS)
                } catch (ie: InterruptedException) {
                    break
                }
            }
        }
        Log.d(tag, "Writer loop ended")
    }

    /**
     * Write final export and stop periodic writing
     *
     * @return Path to final.json if successful, null on error
     */
    fun finalize(): String? {
        try {
            // Stop periodic writer
            running.set(false)
            writerThread?.join(5000)  // Wait up to 5s for graceful shutdown

            val dir = sessionDir ?: return null

            // Export final session state
            val finalJson = try {
                JniBinding.getSessionJson()
            } catch (e: Exception) {
                Log.e(tag, "Failed to export final JSON", e)
                return null
            }

            // Write final export
            val finalFile = storage.writeFinal(dir, finalJson)

            // Parse final export to get stats
            try {
                val gson = Gson()
                val exported = gson.fromJson(finalJson, Map::class.java)
                val metadata = exported["metadata"] as? Map<*, *>
                val accelCount = metadata?.get("accel_sample_count")
                val gyroCount = metadata?.get("gyro_sample_count")
                val gpsCount = metadata?.get("gps_sample_count")

                Log.i(
                    tag,
                    "✓ Session finalized: ${dir.sessionName} " +
                    "(A:$accelCount G:$gyroCount P:$gpsCount, " +
                    "${String.format("%.2f", dir.getSizeMb())}MB)"
                )
            } catch (e: Exception) {
                Log.w(tag, "Failed to parse final export stats", e)
            }

            return finalFile.absolutePath
        } catch (e: Exception) {
            Log.e(tag, "Failed to finalize session", e)
            return null
        }
    }

    /**
     * Get session directory (for accessing paths)
     */
    fun getSessionDirectory(): SessionDirectory? {
        return sessionDir
    }

    /**
     * Get current session name
     */
    fun getSessionName(): String? {
        return sessionDir?.sessionName
    }

    /**
     * Get chunk count
     */
    fun getChunkCount(): Int {
        return chunkIndex
    }

    /**
     * Get session size
     */
    fun getSessionSize(): Long {
        return sessionDir?.getSize() ?: 0L
    }

    /**
     * Stop writing immediately (used on error)
     */
    fun stop() {
        try {
            running.set(false)
            writerThread?.join(2000)
            Log.d(tag, "Session writer stopped")
        } catch (e: Exception) {
            Log.e(tag, "Error stopping writer", e)
        }
    }
}

/**
 * Session export format - mirrors Rust SessionExport struct
 * Contains all samples and metadata for export
 */
data class SessionExportData(
    val metadata: Map<String, Any>,
    val accel_samples: List<Map<String, Double>>,
    val gyro_samples: List<Map<String, Double>>,
    val gps_samples: List<Map<String, Double>>,
    val stats: Map<String, Any>? = null
)
