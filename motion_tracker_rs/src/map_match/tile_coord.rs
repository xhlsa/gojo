use std::fmt::{Display, Formatter};

/// Represents a tile coordinate in Web Mercator projection (slippy map tilenames)
#[derive(Clone, Copy, Hash, Eq, PartialEq, Debug)]
pub struct TileCoord {
    pub x: u32,
    pub y: u32,
    pub zoom: u8,
}

impl TileCoord {
    /// Converts WGS84 lat/lon (degrees) to tile coordinates at given zoom level
    ///
    /// Uses Web Mercator projection (EPSG:3857)
    /// Formula:
    ///   n = 2^zoom
    ///   x = floor((lon + 180) / 360 * n)
    ///   y = floor((1 - ln(tan(lat_rad) + sec(lat_rad)) / π) / 2 * n)
    pub fn from_latlon(lat: f64, lon: f64, zoom: u8) -> Self {
        // Clamp lat to Web Mercator valid range (avoids tan singularity at poles)
        let lat = lat.clamp(-85.05112878, 85.05112878);

        // Wrap lon to [-180, 180]
        let lon = ((lon + 180.0) % 360.0 + 360.0) % 360.0 - 180.0;

        let n = 2u32.pow(zoom as u32) as f64;

        // X coordinate (longitude)
        let x_raw = (lon + 180.0) / 360.0 * n;
        let x = x_raw.floor() as u32;

        // Y coordinate (latitude) - Web Mercator projection
        let lat_rad = lat.to_radians();
        let y_raw = (1.0 - (lat_rad.tan() + 1.0 / lat_rad.cos()).ln() / std::f64::consts::PI) / 2.0 * n;
        let y = y_raw.floor() as u32;

        // Clamp to valid tile range
        let max_tile = 2u32.pow(zoom as u32) - 1;
        TileCoord {
            x: x.min(max_tile),
            y: y.min(max_tile),
            zoom,
        }
    }

    /// Returns bounding box for this tile as (min_lat, min_lon, max_lat, max_lon)
    ///
    /// Inverse of from_latlon - reconstructs lat/lon bounds from tile coords
    pub fn bbox(&self) -> (f64, f64, f64, f64) {
        let n = 2u32.pow(self.zoom as u32) as f64;

        // Longitude bounds (simple linear interpolation)
        let min_lon = self.x as f64 / n * 360.0 - 180.0;
        let max_lon = (self.x + 1) as f64 / n * 360.0 - 180.0;

        // Latitude bounds (inverse Web Mercator)
        let max_lat = mercator_y_to_lat(self.y, n);
        let min_lat = mercator_y_to_lat(self.y + 1, n);

        (min_lat, min_lon, max_lat, max_lon)
    }

    /// Returns the 8 surrounding tiles in a 3x3 grid (excluding center)
    ///
    /// Order: NW, N, NE, W, E, SW, S, SE
    /// Handles x wrapping (longitude), clips y at poles
    pub fn neighbors(&self) -> [TileCoord; 8] {
        let max_tile = 2u32.pow(self.zoom as u32) - 1;

        // Helper to wrap x coordinate (longitude wraps)
        let wrap_x = |x: i32| -> u32 {
            ((x + (max_tile as i32 + 1)) % (max_tile as i32 + 1)) as u32
        };

        // Helper to clamp y coordinate (latitude doesn't wrap)
        let clamp_y = |y: i32| -> u32 {
            y.max(0).min(max_tile as i32) as u32
        };

        let x = self.x as i32;
        let y = self.y as i32;

        [
            // NW
            TileCoord { x: wrap_x(x - 1), y: clamp_y(y - 1), zoom: self.zoom },
            // N
            TileCoord { x: self.x, y: clamp_y(y - 1), zoom: self.zoom },
            // NE
            TileCoord { x: wrap_x(x + 1), y: clamp_y(y - 1), zoom: self.zoom },
            // W
            TileCoord { x: wrap_x(x - 1), y: self.y, zoom: self.zoom },
            // E
            TileCoord { x: wrap_x(x + 1), y: self.y, zoom: self.zoom },
            // SW
            TileCoord { x: wrap_x(x - 1), y: clamp_y(y + 1), zoom: self.zoom },
            // S
            TileCoord { x: self.x, y: clamp_y(y + 1), zoom: self.zoom },
            // SE
            TileCoord { x: wrap_x(x + 1), y: clamp_y(y + 1), zoom: self.zoom },
        ]
    }

    /// Manhattan distance between two tiles (only valid for same zoom level)
    ///
    /// Returns None if zoom levels differ
    pub fn distance_to(&self, other: &TileCoord) -> Option<u32> {
        if self.zoom != other.zoom {
            return None;
        }

        // Handle x-axis wrapping (choose shorter path around sphere)
        let max_tile = 2u32.pow(self.zoom as u32);
        let dx_direct = (self.x as i32 - other.x as i32).unsigned_abs();
        let dx_wrapped = max_tile - dx_direct;
        let dx = dx_direct.min(dx_wrapped);

        // Y-axis doesn't wrap
        let dy = (self.y as i32 - other.y as i32).unsigned_abs();

        Some(dx + dy)
    }
}

impl Display for TileCoord {
    fn fmt(&self, f: &mut Formatter) -> std::fmt::Result {
        write!(f, "tile_{}_{}_z{}", self.x, self.y, self.zoom)
    }
}

/// Helper: Convert Web Mercator y tile coordinate to latitude
fn mercator_y_to_lat(y: u32, n: f64) -> f64 {
    let y_mercator = std::f64::consts::PI * (1.0 - 2.0 * y as f64 / n);
    y_mercator.sinh().atan().to_degrees()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_from_latlon_to_bbox_roundtrip() {
        // Test point: San Francisco
        let lat = 37.7749;
        let lon = -122.4194;
        let zoom = 14;

        let tile = TileCoord::from_latlon(lat, lon, zoom);
        let (min_lat, min_lon, max_lat, max_lon) = tile.bbox();

        // Original point should be within tile bounds
        assert!(lat >= min_lat && lat <= max_lat);
        assert!(lon >= min_lon && lon <= max_lon);

        // Tile bounds should be reasonable size (~5km at Z14)
        assert!((max_lat - min_lat).abs() < 0.05); // Less than ~5.5 km
        assert!((max_lon - min_lon).abs() < 0.05);
    }

    #[test]
    fn test_neighbors_cardinal_directions() {
        let center = TileCoord::from_latlon(0.0, 0.0, 10);
        let neighbors = center.neighbors();

        // North should have y-1
        assert_eq!(neighbors[1].y, center.y.saturating_sub(1));
        assert_eq!(neighbors[1].x, center.x);

        // South should have y+1
        assert_eq!(neighbors[6].y, center.y + 1);
        assert_eq!(neighbors[6].x, center.x);

        // East should have x+1 (or wrap)
        assert_eq!(neighbors[4].y, center.y);

        // West should have x-1 (or wrap)
        assert_eq!(neighbors[3].y, center.y);
    }

    #[test]
    fn test_edge_tiles() {
        let zoom = 10;
        let max_tile = 2u32.pow(zoom as u32) - 1;

        // Test tile at x=0 (wraps west to x=max)
        let west_edge = TileCoord { x: 0, y: 512, zoom };
        let neighbors = west_edge.neighbors();

        // West neighbors should wrap to max_tile
        assert_eq!(neighbors[0].x, max_tile); // NW
        assert_eq!(neighbors[3].x, max_tile); // W
        assert_eq!(neighbors[5].x, max_tile); // SW

        // Test tile at x=max (wraps east to x=0)
        let east_edge = TileCoord { x: max_tile, y: 512, zoom };
        let neighbors = east_edge.neighbors();

        assert_eq!(neighbors[2].x, 0); // NE
        assert_eq!(neighbors[4].x, 0); // E
        assert_eq!(neighbors[7].x, 0); // SE
    }

    #[test]
    fn test_pole_behavior() {
        // Near north pole
        let north_tile = TileCoord::from_latlon(85.0, 0.0, 10);
        let neighbors = north_tile.neighbors();

        // North neighbors should clamp at y=0 (not go negative)
        assert_eq!(neighbors[0].y, 0); // NW
        assert_eq!(neighbors[1].y, 0); // N
        assert_eq!(neighbors[2].y, 0); // NE

        // Near south pole
        let south_tile = TileCoord::from_latlon(-85.0, 0.0, 10);
        let max_tile = 2u32.pow(10) - 1;
        let neighbors = south_tile.neighbors();

        // South neighbors should clamp at max_tile (not exceed)
        assert_eq!(neighbors[5].y, max_tile); // SW
        assert_eq!(neighbors[6].y, max_tile); // S
        assert_eq!(neighbors[7].y, max_tile); // SE
    }

    #[test]
    fn test_distance_same_zoom() {
        let tile1 = TileCoord { x: 10, y: 20, zoom: 10 };
        let tile2 = TileCoord { x: 15, y: 23, zoom: 10 };

        let dist = tile1.distance_to(&tile2).expect("Same zoom");
        assert_eq!(dist, 5 + 3); // |10-15| + |20-23| = 8

        // Different zoom should return None
        let tile3 = TileCoord { x: 15, y: 23, zoom: 11 };
        assert!(tile1.distance_to(&tile3).is_none());
    }

    #[test]
    fn test_distance_wrapping() {
        let zoom = 10;
        let max_tile = 2u32.pow(zoom as u32);

        // Tiles near antimeridian - should use shorter wrapped path
        let tile1 = TileCoord { x: 5, y: 512, zoom };
        let tile2 = TileCoord { x: max_tile - 5, y: 512, zoom };

        let dist = tile1.distance_to(&tile2).expect("Same zoom");
        // Direct: |5 - (max-5)| = max-10
        // Wrapped: max - (max-10) = 10
        assert_eq!(dist, 10); // Should choose wrapped path
    }

    #[test]
    fn test_display_format() {
        let tile = TileCoord { x: 123, y: 456, zoom: 14 };
        assert_eq!(format!("{}", tile), "tile_123_456_z14");
    }

    #[test]
    fn test_lat_lon_clamping() {
        // Test extreme latitudes get clamped to Web Mercator range
        let north_pole = TileCoord::from_latlon(90.0, 0.0, 10);
        let south_pole = TileCoord::from_latlon(-90.0, 0.0, 10);

        // Should not panic, should clamp to valid tiles
        assert!(north_pole.y <= 2u32.pow(10) - 1);
        assert!(south_pole.y <= 2u32.pow(10) - 1);

        // Test longitude wrapping
        let wrap_east = TileCoord::from_latlon(0.0, 190.0, 10);
        let wrap_west = TileCoord::from_latlon(0.0, -190.0, 10);

        // 190° wraps to -170°, -190° wraps to 170°
        assert!(wrap_east.x < 2u32.pow(10));
        assert!(wrap_west.x < 2u32.pow(10));
    }
}
