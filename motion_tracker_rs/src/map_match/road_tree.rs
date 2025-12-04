use super::RoadSegment;
use geo::{Coord, EuclideanDistance, LineString, Point};
use rstar::{RTree, RTreeObject, AABB};

/// Wrapper for RoadSegment with spatial indexing envelope
#[derive(Clone, Debug)]
pub struct SpatialRoadSegment {
    pub segment: RoadSegment,
    pub envelope: AABB<[f64; 2]>,
}

impl RTreeObject for SpatialRoadSegment {
    type Envelope = AABB<[f64; 2]>;

    fn envelope(&self) -> Self::Envelope {
        self.envelope
    }
}

/// R-Tree spatial index for fast nearest-road queries
///
/// # Architecture
/// - Indexes RoadSegments by bounding box (envelope)
/// - Enables O(log n) spatial queries for map matching
/// - Typical usage: 500 segments/tile, ~9 comparisons per query
///
/// # Usage
/// ```no_run
/// use motion_tracker_rs::map_match::{RoadTree, RoadSegment};
///
/// let segments: Vec<RoadSegment> = vec![/* ... */];
/// let tree = RoadTree::from_segments(segments);
///
/// // Find roads within 30m of position
/// let candidates = tree.nearest_segments((37.7749, -122.4194), 30.0);
/// ```
pub struct RoadTree {
    tree: RTree<SpatialRoadSegment>,
    segment_count: usize,
}

impl RoadTree {
    /// Create empty R-Tree
    pub fn new() -> Self {
        RoadTree {
            tree: RTree::new(),
            segment_count: 0,
        }
    }

    /// Build R-Tree from a collection of segments
    ///
    /// # Arguments
    /// * `segments` - Road segments to index
    ///
    /// # Returns
    /// Populated RoadTree ready for spatial queries
    pub fn from_segments(segments: Vec<RoadSegment>) -> Self {
        let spatial_segments: Vec<SpatialRoadSegment> = segments
            .into_iter()
            .map(|segment| {
                let envelope = compute_envelope(&segment.geometry);
                SpatialRoadSegment { segment, envelope }
            })
            .collect();

        let segment_count = spatial_segments.len();

        RoadTree {
            tree: RTree::bulk_load(spatial_segments),
            segment_count,
        }
    }

    /// Add single segment to tree
    ///
    /// # Arguments
    /// * `segment` - Road segment to insert
    pub fn insert(&mut self, segment: RoadSegment) {
        let envelope = compute_envelope(&segment.geometry);
        let spatial_segment = SpatialRoadSegment { segment, envelope };
        self.tree.insert(spatial_segment);
        self.segment_count += 1;
    }

    /// Find all segments within max_distance_m of point
    ///
    /// # Arguments
    /// * `point` - Query position (lat, lon) in degrees
    /// * `max_distance_m` - Search radius in meters
    ///
    /// # Returns
    /// References to RoadSegments within radius, sorted by distance
    ///
    /// # Distance Calculation
    /// Uses approximate haversine conversion: 1 degree ≈ 111,000 meters
    /// Good enough for map matching (30m precision requirement)
    pub fn nearest_segments(&self, point: (f64, f64), max_distance_m: f64) -> Vec<&RoadSegment> {
        // Convert radius from meters to degrees (approximate)
        let radius_deg = max_distance_m / 111_000.0;

        // Create bounding box around point
        let (lat, lon) = point;
        let min_lon = lon - radius_deg;
        let max_lon = lon + radius_deg;
        let min_lat = lat - radius_deg;
        let max_lat = lat + radius_deg;

        // Query R-Tree for envelope intersection
        let envelope = AABB::from_corners([min_lon, min_lat], [max_lon, max_lat]);

        let mut candidates: Vec<(&RoadSegment, f64)> = self.tree
            .locate_in_envelope_intersecting(&envelope)
            .map(|spatial_seg| {
                let dist = point_to_segment_distance(point, &spatial_seg.segment);
                (&spatial_seg.segment, dist)
            })
            .filter(|(_, dist)| *dist <= max_distance_m)
            .collect();

        // Sort by distance (nearest first)
        candidates.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap());

        candidates.into_iter().map(|(seg, _)| seg).collect()
    }

    /// Find all segments within a rectangular bounding box
    ///
    /// # Arguments
    /// * `min_lat`, `min_lon` - Southwest corner in degrees
    /// * `max_lat`, `max_lon` - Northeast corner in degrees
    ///
    /// # Returns
    /// References to RoadSegments whose envelopes intersect bbox
    pub fn segments_in_bbox(
        &self,
        min_lat: f64,
        min_lon: f64,
        max_lat: f64,
        max_lon: f64,
    ) -> Vec<&RoadSegment> {
        let envelope = AABB::from_corners([min_lon, min_lat], [max_lon, max_lat]);

        self.tree
            .locate_in_envelope_intersecting(&envelope)
            .map(|spatial_seg| &spatial_seg.segment)
            .collect()
    }

    /// Total segments in tree
    pub fn segment_count(&self) -> usize {
        self.segment_count
    }
}

impl Default for RoadTree {
    fn default() -> Self {
        Self::new()
    }
}

/// Compute bounding box (envelope) for a LineString
fn compute_envelope(line_string: &LineString<f64>) -> AABB<[f64; 2]> {
    let coords: Vec<&Coord<f64>> = line_string.coords().collect();

    if coords.is_empty() {
        // Degenerate case: return zero-size envelope at origin
        return AABB::from_corners([0.0, 0.0], [0.0, 0.0]);
    }

    let (mut min_lon, mut max_lon, mut min_lat, mut max_lat) = (
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
    );

    for coord in coords {
        min_lon = min_lon.min(coord.x);
        max_lon = max_lon.max(coord.x);
        min_lat = min_lat.min(coord.y);
        max_lat = max_lat.max(coord.y);
    }

    AABB::from_corners([min_lon, min_lat], [max_lon, max_lat])
}

/// Point-to-LineString distance in meters
///
/// # Algorithm
/// 1. Use geo crate's EuclideanDistance (degrees)
/// 2. Convert to meters: 1 degree ≈ 111,000m at equator
///
/// # Note
/// This is approximate but sufficient for map matching.
/// For polar regions, use geo::HaversineDistance instead.
fn point_to_segment_distance(point: (f64, f64), segment: &RoadSegment) -> f64 {
    // geo crate uses Point(lon, lat)
    let point_geo = Point::new(point.1, point.0);

    let dist_degrees = point_geo.euclidean_distance(&segment.geometry);
    dist_degrees * 111_000.0  // Approximate conversion to meters
}

#[cfg(test)]
mod tests {
    use super::*;
    use geo::LineString;

    fn make_test_segment(id: u64, coords: Vec<(f64, f64)>) -> RoadSegment {
        let points: Vec<Coord<f64>> = coords
            .into_iter()
            .map(|(lon, lat)| Coord { x: lon, y: lat })
            .collect();

        RoadSegment {
            id,
            geometry: LineString::new(points),
            heading: 0.0,
            road_class: super::super::RoadClass::Primary,
            one_way: false,
            name: None,
        }
    }

    #[test]
    fn test_build_tree_from_segments() {
        let segments = vec![
            make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]),
            make_test_segment(2, vec![(-122.4200, 37.7760), (-122.4210, 37.7770)]),
        ];

        let tree = RoadTree::from_segments(segments);
        assert_eq!(tree.segment_count(), 2);
    }

    #[test]
    fn test_build_tree_from_100_segments() {
        let mut segments = Vec::new();
        for i in 0..100 {
            let offset = i as f64 * 0.001;
            segments.push(make_test_segment(
                i,
                vec![
                    (-122.4194 + offset, 37.7749 + offset),
                    (-122.4195 + offset, 37.7750 + offset),
                ],
            ));
        }

        let tree = RoadTree::from_segments(segments);
        assert_eq!(tree.segment_count(), 100);
    }

    #[test]
    fn test_nearest_segments_basic() {
        let segments = vec![
            make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]),
            make_test_segment(2, vec![(-122.4200, 37.7760), (-122.4210, 37.7770)]),
            make_test_segment(3, vec![(-122.4300, 37.7850), (-122.4310, 37.7860)]),
        ];

        let tree = RoadTree::from_segments(segments);

        // Query near segment 1
        let candidates = tree.nearest_segments((37.7749, -122.4194), 100.0);
        assert!(!candidates.is_empty());
        assert_eq!(candidates[0].id, 1);
    }

    #[test]
    fn test_nearest_segments_radius_filter() {
        let segments = vec![
            make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]),
            make_test_segment(2, vec![(-122.5000, 37.8000), (-122.5010, 37.8010)]),
        ];

        let tree = RoadTree::from_segments(segments);

        // Query near segment 1 with small radius (should exclude segment 2)
        let candidates = tree.nearest_segments((37.7749, -122.4194), 50.0);
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].id, 1);

        // Query with large radius (should include both)
        let candidates = tree.nearest_segments((37.7749, -122.4194), 20_000.0);
        assert_eq!(candidates.len(), 2);
    }

    #[test]
    fn test_segments_in_bbox() {
        let segments = vec![
            make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]),
            make_test_segment(2, vec![(-122.4200, 37.7760), (-122.4210, 37.7770)]),
            make_test_segment(3, vec![(-122.5000, 37.8000), (-122.5010, 37.8010)]),
        ];

        let tree = RoadTree::from_segments(segments);

        // Query bbox covering segments 1 and 2
        let results = tree.segments_in_bbox(37.774, -122.422, 37.778, -122.418);
        assert_eq!(results.len(), 2);

        // Query bbox covering all segments
        let results = tree.segments_in_bbox(37.770, -122.510, 37.810, -122.410);
        assert_eq!(results.len(), 3);
    }

    #[test]
    fn test_empty_tree() {
        let tree = RoadTree::new();
        assert_eq!(tree.segment_count(), 0);

        let candidates = tree.nearest_segments((37.7749, -122.4194), 100.0);
        assert_eq!(candidates.len(), 0);

        let results = tree.segments_in_bbox(37.770, -122.422, 37.778, -122.418);
        assert_eq!(results.len(), 0);
    }

    #[test]
    fn test_spatial_accuracy() {
        // Create segment at known position
        let segment = make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]);
        let tree = RoadTree::from_segments(vec![segment]);

        // Query exactly on the line (should be within 1m tolerance)
        let candidates = tree.nearest_segments((37.7749, -122.4194), 1.0);
        assert_eq!(candidates.len(), 1);

        // Query 200m away (should not match with 100m radius)
        let candidates = tree.nearest_segments((37.7769, -122.4194), 100.0);
        assert_eq!(candidates.len(), 0);
    }

    #[test]
    fn test_distance_metric() {
        let segment = make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]);

        // Point exactly on start of line: distance ≈ 0
        let dist = point_to_segment_distance((37.7749, -122.4194), &segment);
        assert!(dist < 1.0, "Distance to point on line should be ~0m, got {}", dist);

        // Point ~111m north (0.001 degrees latitude)
        let dist = point_to_segment_distance((37.7759, -122.4194), &segment);
        assert!(dist > 100.0 && dist < 120.0, "Distance should be ~111m, got {}", dist);
    }

    #[test]
    fn test_insert() {
        let mut tree = RoadTree::new();
        assert_eq!(tree.segment_count(), 0);

        let segment = make_test_segment(1, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]);
        tree.insert(segment);
        assert_eq!(tree.segment_count(), 1);

        let candidates = tree.nearest_segments((37.7749, -122.4194), 50.0);
        assert_eq!(candidates.len(), 1);
    }

    #[test]
    fn test_sorted_by_distance() {
        let segments = vec![
            make_test_segment(1, vec![(-122.4300, 37.7800), (-122.4310, 37.7810)]),
            make_test_segment(2, vec![(-122.4194, 37.7749), (-122.4195, 37.7750)]),
            make_test_segment(3, vec![(-122.4250, 37.7780), (-122.4260, 37.7790)]),
        ];

        let tree = RoadTree::from_segments(segments);

        // Query near segment 2 (should be first)
        let candidates = tree.nearest_segments((37.7749, -122.4194), 10_000.0);
        assert_eq!(candidates.len(), 3);
        assert_eq!(candidates[0].id, 2, "Nearest segment should be first");
    }
}
