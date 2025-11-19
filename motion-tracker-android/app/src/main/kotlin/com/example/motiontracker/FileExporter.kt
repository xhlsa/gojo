package com.example.motiontracker

import android.content.Context
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Session file export to internal storage
 *
 * Exports session data (JSON) to device internal storage:
 * - Path: context.getFilesDir()/sessions/
 * - Format: JSON (contains all samples and metadata)
 * - Naming: comparison_TIMESTAMP_final.json (matches Python format)
 *
 * Graceful error handling - logs errors but doesn't crash service
 */
class FileExporter(private val context: Context) {
    private val tag = "MotionTracker.Export"

    companion object {
        private const val SESSION_DIR = "sessions"
        private val dateFormat = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
    }

    /**
     * Export session as JSON file
     *
     * @return File path if successful, null on error
     */
    fun exportSessionJson(sessionJson: String): String? {
        return try {
            // Create sessions directory if needed
            val sessionsDir = File(context.filesDir, SESSION_DIR)
            if (!sessionsDir.exists() && !sessionsDir.mkdirs()) {
                Log.e(tag, "Failed to create sessions directory")
                return null
            }

            // Generate filename: comparison_TIMESTAMP_final.json
            val timestamp = dateFormat.format(Date())
            val filename = "comparison_${timestamp}_final.json"
            val file = File(sessionsDir, filename)

            // Write JSON to file
            file.writeText(sessionJson, Charsets.UTF_8)

            Log.i(tag, "✓ Session exported to ${file.absolutePath}")
            Log.d(tag, "File size: ${file.length()} bytes")

            file.absolutePath
        } catch (e: Exception) {
            Log.e(tag, "Failed to export session", e)
            null
        }
    }

    /**
     * Export session as GPX file (for mapping applications)
     *
     * @param gpsData GPS samples as JSON array
     * @return File path if successful, null on error
     */
    fun exportSessionGpx(sessionId: String, gpsData: String): String? {
        return try {
            val sessionsDir = File(context.filesDir, SESSION_DIR)
            if (!sessionsDir.exists() && !sessionsDir.mkdirs()) {
                Log.e(tag, "Failed to create sessions directory")
                return null
            }

            // Generate filename: tracks_TIMESTAMP_final.gpx
            val timestamp = dateFormat.format(Date())
            val filename = "tracks_${timestamp}_final.gpx"
            val file = File(sessionsDir, filename)

            // Write GPX to file
            file.writeText(gpsData, Charsets.UTF_8)

            Log.i(tag, "✓ GPX track exported to ${file.absolutePath}")
            Log.d(tag, "File size: ${file.length()} bytes")

            file.absolutePath
        } catch (e: Exception) {
            Log.e(tag, "Failed to export GPX", e)
            null
        }
    }

    /**
     * Get list of exported sessions
     *
     * @return List of file paths
     */
    fun listExportedSessions(): List<File> {
        return try {
            val sessionsDir = File(context.filesDir, SESSION_DIR)
            if (!sessionsDir.exists()) {
                return emptyList()
            }

            sessionsDir.listFiles { file ->
                file.isFile && (file.name.endsWith(".json") || file.name.endsWith(".gpx"))
            }?.toList() ?: emptyList()
        } catch (e: Exception) {
            Log.e(tag, "Failed to list sessions", e)
            emptyList()
        }
    }

    /**
     * Delete a session file
     *
     * @param filePath Path to file to delete
     * @return True if deleted, false otherwise
     */
    fun deleteSession(filePath: String): Boolean {
        return try {
            val file = File(filePath)
            if (file.exists() && file.delete()) {
                Log.i(tag, "Deleted session: $filePath")
                true
            } else {
                Log.w(tag, "Failed to delete session: $filePath")
                false
            }
        } catch (e: Exception) {
            Log.e(tag, "Error deleting session", e)
            false
        }
    }

    /**
     * Get session directory path
     *
     * @return Directory path
     */
    fun getSessionsDir(): File {
        return File(context.filesDir, SESSION_DIR).also {
            it.mkdirs()
        }
    }

    /**
     * Get total size of all exported sessions
     *
     * @return Size in bytes
     */
    fun getTotalSessionsSize(): Long {
        return try {
            listExportedSessions().sumOf { it.length() }
        } catch (e: Exception) {
            Log.e(tag, "Failed to calculate sessions size", e)
            0
        }
    }
}

/**
 * Export manager for handling session data export
 * Can be used in Activity or Service to trigger exports
 */
class SessionExportManager(private val context: Context) {
    private val tag = "MotionTracker.ExportMgr"
    private val exporter = FileExporter(context)

    /**
     * Export current session and return file path
     *
     * @return Export result containing path and status
     */
    fun exportCurrentSession(): ExportResult {
        return try {
            // Get session JSON from Rust
            val jsonData = try {
                JniBinding.getSessionJson()
            } catch (e: Exception) {
                Log.e(tag, "Failed to get session JSON from Rust", e)
                return ExportResult(
                    success = false,
                    message = "Failed to export from Rust: ${e.message}",
                    filePath = null
                )
            }

            // Write to file
            val filePath = exporter.exportSessionJson(jsonData)
            if (filePath != null) {
                ExportResult(
                    success = true,
                    message = "Session exported successfully",
                    filePath = filePath
                )
            } else {
                ExportResult(
                    success = false,
                    message = "Failed to write session to file",
                    filePath = null
                )
            }
        } catch (e: Exception) {
            Log.e(tag, "Unexpected error during export", e)
            ExportResult(
                success = false,
                message = "Unexpected error: ${e.message}",
                filePath = null
            )
        }
    }

    /**
     * List all exported sessions
     *
     * @return List of session files
     */
    fun listSessions(): List<SessionInfo> {
        return exporter.listExportedSessions().map { file ->
            SessionInfo(
                name = file.name,
                path = file.absolutePath,
                sizeBytes = file.length(),
                lastModified = file.lastModified()
            )
        }
    }

    /**
     * Delete a session file
     */
    fun deleteSession(filePath: String): Boolean {
        return exporter.deleteSession(filePath)
    }

    /**
     * Get sessions directory size
     */
    fun getTotalSize(): Long {
        return exporter.getTotalSessionsSize()
    }
}

/**
 * Result of export operation
 */
data class ExportResult(
    val success: Boolean,
    val message: String,
    val filePath: String?
)

/**
 * Info about exported session file
 */
data class SessionInfo(
    val name: String,
    val path: String,
    val sizeBytes: Long,
    val lastModified: Long
) {
    fun sizeKb(): Double = sizeBytes / 1024.0
    fun sizeMb(): Double = sizeBytes / 1024.0 / 1024.0
}
