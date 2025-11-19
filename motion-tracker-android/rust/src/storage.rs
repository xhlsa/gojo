use serde::{Deserialize, Serialize};
use crate::sensor_receiver::{AccelSample, GyroSample, GpsSample};
use crate::session::SessionMetadata;

/// Complete session export (JSON-serializable)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionExport {
    pub metadata: SessionMetadata,
    pub accel_samples: Vec<AccelSample>,
    pub gyro_samples: Vec<GyroSample>,
    pub gps_samples: Vec<GpsSample>,
}

impl SessionExport {
    /// Serialize to JSON string
    pub fn to_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string_pretty(self)
    }

    /// Serialize to JSON bytes
    pub fn to_json_bytes(&self) -> Result<Vec<u8>, serde_json::Error> {
        serde_json::to_vec_pretty(self)
    }

    /// Get session size in bytes (approximate)
    pub fn size_bytes(&self) -> usize {
        self.accel_samples.len() * 32 +
        self.gyro_samples.len() * 32 +
        self.gps_samples.len() * 64 +
        1024  // metadata + overhead
    }
}

/// GPX track format for mapping applications
#[derive(Debug, Serialize)]
pub struct GpxTrack {
    pub name: String,
    pub description: String,
    pub track_points: Vec<GpxPoint>,
}

#[derive(Debug, Serialize)]
pub struct GpxPoint {
    pub lat: f64,
    pub lon: f64,
    pub ele: f64,  // elevation
    pub time: String,
    pub hdop: f64,  // horizontal dilution of precision
}

impl GpxTrack {
    /// Generate GPX document XML string
    pub fn to_gpx_xml(&self) -> String {
        let mut xml = String::new();
        xml.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
        xml.push_str("<gpx version=\"1.1\" creator=\"MotionTracker\">\n");
        xml.push_str("  <metadata>\n");
        xml.push_str(&format!("    <name>{}</name>\n", self.name));
        xml.push_str(&format!("    <desc>{}</desc>\n", self.description));
        xml.push_str("  </metadata>\n");
        xml.push_str("  <trk>\n");
        xml.push_str(&format!("    <name>{}</name>\n", self.name));
        xml.push_str("    <trkseg>\n");

        for point in &self.track_points {
            xml.push_str(&format!("      <trkpt lat=\"{}\" lon=\"{}\">\n", point.lat, point.lon));
            xml.push_str(&format!("        <ele>{}</ele>\n", point.ele));
            xml.push_str(&format!("        <time>{}</time>\n", point.time));
            xml.push_str("      </trkpt>\n");
        }

        xml.push_str("    </trkseg>\n");
        xml.push_str("  </trk>\n");
        xml.push_str("</gpx>\n");

        xml
    }
}

/// Create GPX track from GPS samples
pub fn create_gpx_track(
    session_id: &str,
    start_time: &str,
    gps_samples: &[GpsSample],
) -> GpxTrack {
    let track_points = gps_samples
        .iter()
        .map(|sample| {
            // Convert timestamp to ISO8601 (simplified)
            let time = chrono::DateTime::<chrono::Utc>::from(
                std::time::UNIX_EPOCH + std::time::Duration::from_secs_f64(sample.timestamp)
            ).to_rfc3339();

            GpxPoint {
                lat: sample.latitude,
                lon: sample.longitude,
                ele: sample.altitude,
                time,
                hdop: sample.accuracy / 2.0,  // Approximate HDOP from accuracy
            }
        })
        .collect();

    GpxTrack {
        name: format!("Motion Track {}", session_id),
        description: format!("Recorded from {}", start_time),
        track_points,
    }
}

/// Session statistics for display
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SessionStats {
    pub duration_seconds: f64,
    pub accel_sample_count: u32,
    pub gyro_sample_count: u32,
    pub gps_fix_count: u32,
    pub total_distance_meters: f64,
    pub peak_speed_ms: f64,
    pub peak_speed_kmh: f64,
}

impl SessionStats {
    pub fn from_metadata(meta: &SessionMetadata) -> Self {
        SessionStats {
            duration_seconds: 0.0,  // Would need timestamp to calculate
            accel_sample_count: meta.accel_sample_count,
            gyro_sample_count: meta.gyro_sample_count,
            gps_fix_count: meta.gps_sample_count,
            total_distance_meters: meta.distance_meters,
            peak_speed_ms: meta.peak_speed_ms,
            peak_speed_kmh: meta.peak_speed_ms * 3.6,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_export_json_serialization() {
        use crate::session::SessionState;

        let metadata = SessionMetadata {
            session_id: "test_session".to_string(),
            start_time: "2025-11-19T12:00:00Z".to_string(),
            state: SessionState::Idle,
            accel_sample_count: 100,
            gyro_sample_count: 100,
            gps_sample_count: 10,
            distance_meters: 500.0,
            peak_speed_ms: 20.0,
        };

        let accel = vec![AccelSample::new(1.0, 2.0, 3.0, 0.0)];
        let gyro = vec![GyroSample::new(0.1, 0.2, 0.3, 0.0)];
        let gps = vec![GpsSample::new(40.0, -120.0, 100.0, 5.0, 15.0, 90.0, 0.0)];

        let export = SessionExport {
            metadata,
            accel_samples: accel,
            gyro_samples: gyro,
            gps_samples: gps,
        };

        let json = export.to_json().unwrap();
        assert!(json.contains("test_session"));
        assert!(json.contains("40"));  // latitude
    }

    #[test]
    fn test_gpx_generation() {
        let gps = vec![
            GpsSample::new(40.0, -120.0, 100.0, 5.0, 15.0, 90.0, 0.0),
            GpsSample::new(40.01, -120.01, 105.0, 5.0, 15.0, 90.0, 1.0),
        ];

        let track = create_gpx_track("test", "2025-11-19T12:00:00Z", &gps);
        let gpx_xml = track.to_gpx_xml();

        assert!(gpx_xml.contains("40"));
        assert!(gpx_xml.contains("-120"));
        assert!(gpx_xml.contains("gpx"));
    }
}
