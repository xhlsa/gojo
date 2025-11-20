package com.example.motiontracker.storage

import android.content.Context
import android.util.Log
import com.example.motiontracker.data.SessionConfig
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * SessionStorage - Manages app-specific storage for motion tracking sessions
 *
 * Directory structure:
 * /sdcard/Android/data/<package>/files/sessions/
 *   └── session_20241119_143022/
 *       ├── metadata.json                    (session config + timing)
 *       ├── chunks/
 *       │   ├── chunk_0.json                 (0-15s)
 *       │   ├── chunk_1.json                 (15-30s)
 *       │   └── ...
 *       └── final.json                       (complete session export at end)
 *
 * Chunk format: JSON with [accel_samples], [gyro_samples], [gps_samples]
 * Final format: Full SessionExport (metadata + all samples + stats)
 */
class SessionStorage(private val context: Context) {
    private val tag = "MotionTracker.Storage"
    private val dateFormat = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)

    /**
     * Create a new session directory
     *
     * @param config Session configuration
     * @return SessionDirectory with all paths initialized
     */
    fun createSessionDirectory(config: SessionConfig): SessionDirectory {
        val timestamp = dateFormat.format(Date())
        val sessionName = "session_$timestamp"
        val sessionsRootDir = File(context.filesDir, "sessions")
        val sessionDir = File(sessionsRootDir, sessionName)
        val chunksDir = File(sessionDir, "chunks")

        // Create directories
        if (!sessionsRootDir.exists() && !sessionsRootDir.mkdirs()) {
            throw RuntimeException("Failed to create sessions root directory")
        }
        if (!sessionDir.exists() && !sessionDir.mkdirs()) {
            throw RuntimeException("Failed to create session directory")
        }
        if (!chunksDir.exists() && !chunksDir.mkdirs()) {
            throw RuntimeException("Failed to create chunks directory")
        }

        Log.i(tag, "✓ Created session directory: ${sessionDir.absolutePath}")

        return SessionDirectory(
            sessionDir = sessionDir,
            chunksDir = chunksDir,
            metadataFile = File(sessionDir, "metadata.json"),
            finalFile = File(sessionDir, "final.json"),
            sessionName = sessionName,
            createdAt = System.currentTimeMillis()
        )
    }

    /**
     * Write metadata file (session config + timing)
     */
    fun writeMetadata(sessionDir: SessionDirectory, config: SessionConfig, startTime: Long) {
        try {
            val metadata = mapOf(
                "session_name" to sessionDir.sessionName,
                "start_time" to startTime,
                "created_at" to sessionDir.createdAt,
                "device_model" to config.deviceModel,
                "device_manufacturer" to config.deviceManufacturer,
                "os_version" to config.osVersion,
                "accel_rate_hz" to config.accelRateHz,
                "gyro_rate_hz" to config.gyroRateHz,
                "gps_rate_hz" to config.gpsRateHz
            )

            val json = com.google.gson.Gson().toJson(metadata)
            sessionDir.metadataFile.writeText(json, Charsets.UTF_8)

            Log.d(tag, "✓ Metadata written: ${sessionDir.metadataFile.absolutePath}")
        } catch (e: Exception) {
            Log.e(tag, "Failed to write metadata", e)
            throw e
        }
    }

    /**
     * Write a chunk file (periodic data snapshot)
     *
     * @param sessionDir Session directory
     * @param chunkIndex Chunk number (0, 1, 2, ...)
     * @param chunkJson JSON with samples
     */
    fun writeChunk(sessionDir: SessionDirectory, chunkIndex: Int, chunkJson: String): File {
        try {
            val chunkFile = File(sessionDir.chunksDir, "chunk_$chunkIndex.json")
            chunkFile.writeText(chunkJson, Charsets.UTF_8)

            Log.d(tag, "✓ Chunk $chunkIndex written: ${chunkFile.absolutePath} (${chunkFile.length()} bytes)")
            return chunkFile
        } catch (e: Exception) {
            Log.e(tag, "Failed to write chunk $chunkIndex", e)
            throw e
        }
    }

    /**
     * Write final session export (complete data + stats)
     *
     * @param sessionDir Session directory
     * @param finalJson Complete session export JSON
     */
    fun writeFinal(sessionDir: SessionDirectory, finalJson: String): File {
        try {
            sessionDir.finalFile.writeText(finalJson, Charsets.UTF_8)

            Log.i(tag, "✓ Final export written: ${sessionDir.finalFile.absolutePath} (${sessionDir.finalFile.length()} bytes)")
            return sessionDir.finalFile
        } catch (e: Exception) {
            Log.e(tag, "Failed to write final export", e)
            throw e
        }
    }

    /**
     * Get all exported sessions
     *
     * @return List of SessionDirectory objects
     */
    fun listSessions(): List<SessionDirectory> {
        return try {
            val sessionsRootDir = File(context.filesDir, "sessions")
            if (!sessionsRootDir.exists()) {
                return emptyList()
            }

            sessionsRootDir.listFiles { file ->
                file.isDirectory && file.name.startsWith("session_")
            }?.map { sessionDir ->
                SessionDirectory(
                    sessionDir = sessionDir,
                    chunksDir = File(sessionDir, "chunks"),
                    metadataFile = File(sessionDir, "metadata.json"),
                    finalFile = File(sessionDir, "final.json"),
                    sessionName = sessionDir.name,
                    createdAt = sessionDir.lastModified()
                )
            } ?: emptyList()
        } catch (e: Exception) {
            Log.e(tag, "Failed to list sessions", e)
            emptyList()
        }
    }

    /**
     * Delete a session directory
     *
     * @param sessionDir Session directory to delete
     * @return True if deleted, false otherwise
     */
    fun deleteSession(sessionDir: SessionDirectory): Boolean {
        return try {
            val deleted = sessionDir.sessionDir.deleteRecursively()
            if (deleted) {
                Log.i(tag, "✓ Deleted session: ${sessionDir.sessionName}")
            } else {
                Log.w(tag, "Failed to delete session: ${sessionDir.sessionName}")
            }
            deleted
        } catch (e: Exception) {
            Log.e(tag, "Error deleting session", e)
            false
        }
    }

    /**
     * Get total size of all session data
     *
     * @return Size in bytes
     */
    fun getTotalSize(): Long {
        return try {
            listSessions().sumOf { sessionDir ->
                sessionDir.sessionDir.walk().sumOf { it.length() }
            }
        } catch (e: Exception) {
            Log.e(tag, "Failed to calculate total size", e)
            0L
        }
    }

    /**
     * Check available disk space
     *
     * @return Available bytes
     */
    fun getAvailableSpace(): Long {
        return try {
            val stat = android.os.StatFs(context.filesDir.absolutePath)
            stat.availableBlocksLong * stat.blockSizeLong
        } catch (e: Exception) {
            Log.e(tag, "Failed to check available space", e)
            0L
        }
    }
}

/**
 * SessionDirectory - Represents a single session's directory structure
 */
data class SessionDirectory(
    val sessionDir: File,
    val chunksDir: File,
    val metadataFile: File,
    val finalFile: File,
    val sessionName: String,
    val createdAt: Long
) {
    /**
     * Get list of all chunks in this session
     */
    fun getChunks(): List<File> {
        return chunksDir.listFiles { file ->
            file.isFile && file.name.startsWith("chunk_") && file.name.endsWith(".json")
        }?.sortedBy { file ->
            // Extract chunk index and sort numerically
            file.name.removePrefix("chunk_").removeSuffix(".json").toIntOrNull() ?: 0
        } ?: emptyList()
    }

    /**
     * Get session size in bytes
     */
    fun getSize(): Long {
        return sessionDir.walk().sumOf { it.length() }
    }

    /**
     * Get session size in MB
     */
    fun getSizeMb(): Double {
        return getSize() / (1024.0 * 1024.0)
    }

    /**
     * Get chunk count
     */
    fun getChunkCount(): Int {
        return getChunks().size
    }

    /**
     * Check if session is complete (has final.json)
     */
    fun isComplete(): Boolean {
        return finalFile.exists()
    }
}
