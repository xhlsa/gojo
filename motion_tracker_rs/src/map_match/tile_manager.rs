use super::{RoadSegment, TileCoord};
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use flate2::Compression;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime};

/// Serializable wrapper for RoadSegment (geo types don't derive Serialize)
#[derive(Clone, Debug, Serialize, Deserialize)]
struct SerializableSegment {
    id: u64,
    coords: Vec<(f64, f64)>,
    heading: f64,
    road_class: super::RoadClass,
    one_way: bool,
    name: Option<String>,
}

impl From<&RoadSegment> for SerializableSegment {
    fn from(seg: &RoadSegment) -> Self {
        SerializableSegment {
            id: seg.id,
            coords: seg.geometry.0.iter().map(|c| (c.x, c.y)).collect(),
            heading: seg.heading,
            road_class: seg.road_class,
            one_way: seg.one_way,
            name: seg.name.clone(),
        }
    }
}

impl From<SerializableSegment> for RoadSegment {
    fn from(ser: SerializableSegment) -> Self {
        use geo::LineString;

        let points: Vec<geo::Coord<f64>> = ser.coords.into_iter()
            .map(|(x, y)| geo::Coord { x, y })
            .collect();

        RoadSegment {
            id: ser.id,
            geometry: LineString::new(points),
            heading: ser.heading,
            road_class: ser.road_class,
            one_way: ser.one_way,
            name: ser.name,
        }
    }
}

/// Tile data with metadata for cache management
pub struct TileData {
    pub coord: TileCoord,
    pub segments: Vec<RoadSegment>,
    pub loaded_at: Instant,
    pub cached: bool,
}

/// Manages in-memory cache of map tiles with disk persistence
///
/// # Architecture
/// - In-memory HashMap of active tiles (27 tile limit for 3x3 + margin)
/// - LRU eviction based on distance from current tile
/// - Disk cache using gzip-compressed JSON
/// - Non-blocking position updates
///
/// # File Format
/// Cache files: `{cache_dir}/tile_{x}_{y}_z{zoom}.json.gz`
/// - JSON array of serialized RoadSegments
/// - Gzip compression (typical: 500KB -> 100KB)
///
/// # Usage
/// ```no_run
/// use motion_tracker_rs::map_match::TileManager;
/// use std::path::PathBuf;
///
/// let mut manager = TileManager::new(
///     PathBuf::from("/sdcard/gojo/map_cache"),
///     37.7749,
///     -122.4194,
///     14
/// );
///
/// // Update position frequently (called from main loop)
/// manager.update_position(37.7750, -122.4195);
///
/// // Query segments for map matching
/// if let Some(segments) = manager.get_segments(manager.current_tile()) {
///     // Process road segments...
/// }
/// ```
pub struct TileManager {
    cache_dir: PathBuf,
    active_tiles: HashMap<TileCoord, TileData>,
    current_tile: TileCoord,
    zoom_level: u8,
    max_tiles: usize,
}

impl TileManager {
    /// Create new TileManager with cache directory
    ///
    /// # Arguments
    /// * `cache_dir` - Directory for persistent tile cache
    /// * `initial_lat` - Starting latitude for initial tile
    /// * `initial_lon` - Starting longitude for initial tile
    /// * `zoom` - Zoom level (default 14)
    pub fn new(cache_dir: PathBuf, initial_lat: f64, initial_lon: f64, zoom: u8) -> Self {
        // Create cache directory if missing
        if !cache_dir.exists() {
            fs::create_dir_all(&cache_dir)
                .unwrap_or_else(|e| eprintln!("Failed to create cache dir: {}", e));
        }

        let current_tile = TileCoord::from_latlon(initial_lat, initial_lon, zoom);

        let mut manager = TileManager {
            cache_dir,
            active_tiles: HashMap::new(),
            current_tile,
            zoom_level: zoom,
            max_tiles: 27,
        };

        // Preload initial 3x3 grid
        manager.preload_neighborhood(current_tile);

        manager
    }

    /// Update current position and preload tiles if needed
    ///
    /// Non-blocking: checks if tile changed, preloads new neighborhood
    pub fn update_position(&mut self, lat: f64, lon: f64) {
        let new_tile = TileCoord::from_latlon(lat, lon, self.zoom_level);

        if new_tile != self.current_tile {
            self.current_tile = new_tile;
            self.preload_neighborhood(new_tile);
            self.evict_distant_tiles();
        }
    }

    /// Get road segments for a specific tile
    pub fn get_segments(&self, tile: TileCoord) -> Option<&Vec<RoadSegment>> {
        self.active_tiles.get(&tile).map(|data| &data.segments)
    }

    /// Save tile to disk as compressed JSON
    pub fn save_tile_to_disk(
        &self,
        coord: TileCoord,
        segments: &[RoadSegment],
    ) -> Result<(), String> {
        let filepath = self.tile_filepath(coord);

        // Create parent directory if needed
        if let Some(parent) = filepath.parent() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create cache dir: {}", e))?;
        }

        // Convert to serializable format
        let serializable: Vec<SerializableSegment> = segments.iter()
            .map(SerializableSegment::from)
            .collect();

        // Serialize to JSON
        let json = serde_json::to_string(&serializable)
            .map_err(|e| format!("Failed to serialize segments: {}", e))?;

        // Compress with gzip
        let file = File::create(&filepath)
            .map_err(|e| format!("Failed to create cache file: {}", e))?;
        let mut encoder = GzEncoder::new(file, Compression::default());
        encoder.write_all(json.as_bytes())
            .map_err(|e| format!("Failed to write compressed data: {}", e))?;
        encoder.finish()
            .map_err(|e| format!("Failed to finalize compression: {}", e))?;

        Ok(())
    }

    /// Load tile from disk cache
    pub fn load_tile_from_disk(&self, coord: TileCoord) -> Result<Vec<RoadSegment>, String> {
        let filepath = self.tile_filepath(coord);

        // If file doesn't exist, return empty vec (not an error)
        if !filepath.exists() {
            return Ok(Vec::new());
        }

        // Decompress gzip
        let file = File::open(&filepath)
            .map_err(|e| format!("Failed to open cache file: {}", e))?;
        let mut decoder = GzDecoder::new(file);
        let mut json = String::new();
        decoder.read_to_string(&mut json)
            .map_err(|e| format!("Failed to decompress cache: {}", e))?;

        // Deserialize JSON
        let serializable: Vec<SerializableSegment> = serde_json::from_str(&json)
            .map_err(|e| format!("Failed to deserialize segments: {}", e))?;

        // Convert to RoadSegment
        let segments: Vec<RoadSegment> = serializable.into_iter()
            .map(RoadSegment::from)
            .collect();

        Ok(segments)
    }

    /// Clear cache files older than max_age_days
    pub fn clear_old_cache(&self, max_age_days: u32) -> Result<(), String> {
        let max_age = Duration::from_secs(max_age_days as u64 * 86400);
        let now = SystemTime::now();

        // Read cache directory
        let entries = fs::read_dir(&self.cache_dir)
            .map_err(|e| format!("Failed to read cache dir: {}", e))?;

        for entry in entries {
            let entry = entry.map_err(|e| format!("Failed to read entry: {}", e))?;
            let path = entry.path();

            // Only process .json.gz files
            if !path.extension().map(|e| e == "gz").unwrap_or(false) {
                continue;
            }

            // Check file age
            if let Ok(metadata) = fs::metadata(&path) {
                if let Ok(modified) = metadata.modified() {
                    if let Ok(age) = now.duration_since(modified) {
                        if age > max_age {
                            fs::remove_file(&path)
                                .unwrap_or_else(|e| eprintln!("Failed to delete old cache: {}", e));
                        }
                    }
                }
            }
        }

        Ok(())
    }

    /// Get filepath for a tile
    fn tile_filepath(&self, coord: TileCoord) -> PathBuf {
        self.cache_dir.join(format!("{}.json.gz", coord))
    }

    /// Preload 3x3 grid around center tile
    fn preload_neighborhood(&mut self, center: TileCoord) {
        // Center tile
        self.load_tile_if_missing(center);

        // 8 neighbors
        for neighbor in center.neighbors() {
            self.load_tile_if_missing(neighbor);
        }
    }

    /// Load tile from disk if not already in memory
    fn load_tile_if_missing(&mut self, coord: TileCoord) {
        if self.active_tiles.contains_key(&coord) {
            return;
        }

        // Try loading from disk
        match self.load_tile_from_disk(coord) {
            Ok(segments) => {
                self.active_tiles.insert(coord, TileData {
                    coord,
                    segments,
                    loaded_at: Instant::now(),
                    cached: true,
                });
            }
            Err(e) => {
                eprintln!("Failed to load tile {}: {}", coord, e);
                // Insert empty tile to avoid repeated load attempts
                self.active_tiles.insert(coord, TileData {
                    coord,
                    segments: Vec::new(),
                    loaded_at: Instant::now(),
                    cached: false,
                });
            }
        }
    }

    /// Evict tiles outside neighborhood using LRU
    fn evict_distant_tiles(&mut self) {
        while self.active_tiles.len() > self.max_tiles {
            // Find tile furthest from current tile
            let furthest = self.active_tiles
                .keys()
                .filter_map(|&coord| {
                    coord.distance_to(&self.current_tile)
                        .map(|dist| (coord, dist))
                })
                .max_by_key(|(_, dist)| *dist);

            if let Some((coord, dist)) = furthest {
                // Don't evict tiles in 3x3 core neighborhood (dist <= 2)
                if dist <= 2 {
                    break;
                }

                // Save to disk if not already cached
                if let Some(tile) = self.active_tiles.get(&coord) {
                    if !tile.cached && !tile.segments.is_empty() {
                        if let Err(e) = self.save_tile_to_disk(coord, &tile.segments) {
                            eprintln!("Failed to save tile {} before eviction: {}", coord, e);
                        }
                    }
                }

                self.active_tiles.remove(&coord);
            } else {
                break;
            }
        }
    }

    /// Insert tile data (for use by async loader in Phase 4)
    pub fn insert_tile(&mut self, coord: TileCoord, segments: Vec<RoadSegment>) {
        self.active_tiles.insert(coord, TileData {
            coord,
            segments,
            loaded_at: Instant::now(),
            cached: false,
        });
    }

    /// Get current tile coordinate
    pub fn current_tile(&self) -> TileCoord {
        self.current_tile
    }

    /// Get number of active tiles in memory
    pub fn tile_count(&self) -> usize {
        self.active_tiles.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::env;

    #[test]
    fn test_tilecord_to_filepath() {
        let cache_dir = PathBuf::from("/tmp/test_cache");
        let manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);

        let coord = TileCoord { x: 2620, y: 6332, zoom: 14 };
        let filepath = manager.tile_filepath(coord);

        assert_eq!(filepath, cache_dir.join("tile_2620_6332_z14.json.gz"));
    }

    #[test]
    fn test_save_load_roundtrip() {
        use geo::LineString;

        // Create temp cache dir
        let cache_dir = env::temp_dir().join("gojo_test_cache");
        fs::create_dir_all(&cache_dir).unwrap();

        let manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);
        let coord = TileCoord { x: 100, y: 200, zoom: 14 };

        // Create test segments
        let segments = vec![
            RoadSegment {
                id: 1,
                geometry: LineString::new(vec![
                    geo::Coord { x: -122.4194, y: 37.7749 },
                    geo::Coord { x: -122.4195, y: 37.7750 },
                ]),
                heading: 45.0,
                road_class: super::super::RoadClass::Primary,
                one_way: false,
                name: Some("Test Street".to_string()),
            },
        ];

        // Save to disk
        manager.save_tile_to_disk(coord, &segments).unwrap();

        // Load back
        let loaded = manager.load_tile_from_disk(coord).unwrap();

        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].id, 1);
        assert_eq!(loaded[0].heading, 45.0);
        assert_eq!(loaded[0].road_class, super::super::RoadClass::Primary);
        assert_eq!(loaded[0].name, Some("Test Street".to_string()));

        // Cleanup
        fs::remove_dir_all(&cache_dir).unwrap();
    }

    #[test]
    fn test_lru_eviction() {
        let cache_dir = env::temp_dir().join("gojo_lru_test");
        fs::create_dir_all(&cache_dir).unwrap();

        let mut manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);
        manager.max_tiles = 27;

        // Add 30 tiles (should trigger eviction)
        for i in 0..30 {
            let coord = TileCoord { x: i * 10, y: i * 10, zoom: 14 };
            manager.insert_tile(coord, Vec::new());
        }

        // Force eviction
        manager.evict_distant_tiles();

        // Should have evicted down to max_tiles
        assert!(manager.tile_count() <= 27);

        // Cleanup
        fs::remove_dir_all(&cache_dir).unwrap();
    }

    #[test]
    fn test_position_update_preloads() {
        let cache_dir = env::temp_dir().join("gojo_preload_test");
        fs::create_dir_all(&cache_dir).unwrap();

        let mut manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);

        // Initial tile count (3x3 = 9 tiles)
        let initial_count = manager.tile_count();
        assert!(initial_count >= 9);

        // Move to new location (should load new 3x3 grid)
        manager.update_position(37.8, -122.5);

        // Should still have tiles loaded
        assert!(manager.tile_count() >= 9);

        // Cleanup
        fs::remove_dir_all(&cache_dir).unwrap();
    }

    #[test]
    fn test_cache_dir_creation() {
        let cache_dir = env::temp_dir().join("gojo_new_cache");

        // Ensure it doesn't exist
        if cache_dir.exists() {
            fs::remove_dir_all(&cache_dir).unwrap();
        }

        // Create manager (should create directory)
        let _manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);

        // Verify directory was created
        assert!(cache_dir.exists());

        // Cleanup
        fs::remove_dir_all(&cache_dir).unwrap();
    }

    #[test]
    fn test_missing_tile_returns_empty() {
        let cache_dir = env::temp_dir().join("gojo_missing_test");
        fs::create_dir_all(&cache_dir).unwrap();

        let manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);
        let coord = TileCoord { x: 9999, y: 9999, zoom: 14 };

        // Load non-existent tile (should return empty vec, not error)
        let result = manager.load_tile_from_disk(coord);
        assert!(result.is_ok());
        assert_eq!(result.unwrap().len(), 0);

        // Cleanup
        fs::remove_dir_all(&cache_dir).unwrap();
    }

    #[test]
    fn test_get_segments() {
        let cache_dir = env::temp_dir().join("gojo_get_test");
        fs::create_dir_all(&cache_dir).unwrap();

        let mut manager = TileManager::new(cache_dir.clone(), 37.7749, -122.4194, 14);
        let coord = TileCoord { x: 100, y: 200, zoom: 14 };

        // Insert tile
        manager.insert_tile(coord, Vec::new());

        // Should be retrievable
        assert!(manager.get_segments(coord).is_some());

        // Non-existent tile should return None
        let missing = TileCoord { x: 9999, y: 9999, zoom: 14 };
        assert!(manager.get_segments(missing).is_none());

        // Cleanup
        fs::remove_dir_all(&cache_dir).unwrap();
    }
}
