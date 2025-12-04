use super::{RoadSegment, RoadTree};
use geo::{Coord, LineString};
use std::collections::VecDeque;
use std::sync::Arc;

/// Result of map matching a vehicle position to a road segment
#[derive(Clone, Debug)]
pub struct MatchResult {
    pub segment: RoadSegment,
    pub cross_track_error: f64,      // Signed perpendicular distance (m)
    pub along_track_position: f64,   // Normalized [0,1] along segment
    pub heading_error: f64,          // Angular difference (radians)
    pub confidence: f64,             // 0.0-1.0 composite score
    pub distance_to_endpoint: f64,   // For transition detection
}

/// Map matching engine with spatial indexing and hysteresis
///
/// # Architecture
/// - Queries R-Tree for nearby road candidates
/// - Scores matches based on cross-track distance, heading alignment, road class
/// - Applies hysteresis to prevent erratic switches between parallel roads
///
/// # Hysteresis Logic
/// - Holds current match for `match_hold_time_secs` (default: 3s)
/// - Requires new candidate to exceed confidence by `match_switch_threshold` (default: 0.15)
/// - High-speed override (>20 m/s): switches immediately (assume highway merge/exit)
///
/// # Usage
/// ```no_run
/// use motion_tracker_rs::map_match::{RoadTree, MapMatcher};
/// use std::sync::Arc;
///
/// let tree = Arc::new(RoadTree::from_segments(segments));
/// let mut matcher = MapMatcher::new(tree, 30.0);
///
/// if let Some(result) = matcher.match_position((lat, lon), heading, speed, timestamp) {
///     println!("Matched: {}, confidence: {:.2}",
///         result.segment.name.unwrap_or_default(), result.confidence);
/// }
/// ```
pub struct MapMatcher {
    tree: Arc<RoadTree>,
    last_match: Option<MatchResult>,
    match_hold_time_secs: f64,
    last_match_time: f64,
    match_switch_threshold: f64,
    max_search_radius_m: f64,
    position_history: VecDeque<(f64, f64, f64)>,  // (lat, lon, time)
}

impl MapMatcher {
    /// Create new MapMatcher
    ///
    /// # Arguments
    /// * `tree` - R-Tree spatial index of road segments
    /// * `max_radius` - Maximum search radius in meters
    ///
    /// # Defaults
    /// - Hold time: 3 seconds
    /// - Switch threshold: 0.15 confidence delta
    /// - Position history: 10 samples
    pub fn new(tree: Arc<RoadTree>, max_radius: f64) -> Self {
        MapMatcher {
            tree,
            last_match: None,
            match_hold_time_secs: 3.0,
            last_match_time: 0.0,
            match_switch_threshold: 0.15,
            max_search_radius_m: max_radius,
            position_history: VecDeque::with_capacity(10),
        }
    }

    /// Match vehicle position to road network
    ///
    /// # Arguments
    /// * `position` - (lat, lon) in degrees
    /// * `heading` - Vehicle heading in radians [0, 2π]
    /// * `speed` - Vehicle speed in m/s
    /// * `timestamp` - Current time in seconds
    ///
    /// # Returns
    /// Some(MatchResult) if road found within search radius, else None
    pub fn match_position(
        &mut self,
        position: (f64, f64),
        heading: f64,
        speed: f64,
        timestamp: f64,
    ) -> Option<MatchResult> {
        // Reject GPS spikes (>50 m/s = 112 mph)
        if speed > 50.0 {
            return None;
        }

        // Update position history
        self.position_history.push_back((position.0, position.1, timestamp));
        if self.position_history.len() > 10 {
            self.position_history.pop_front();
        }

        // Query R-Tree for nearby segments
        let candidates = self.tree.nearest_segments(position, self.max_search_radius_m);

        if candidates.is_empty() {
            // Off-road: clear match
            self.last_match = None;
            return None;
        }

        // Score all candidates
        let mut scored_matches: Vec<MatchResult> = candidates
            .into_iter()
            .filter_map(|segment| {
                let (cross_track, along_track) =
                    Self::point_to_line_signed_distance(position, segment);

                // Reject if beyond search radius
                if cross_track.abs() > self.max_search_radius_m {
                    return None;
                }

                let heading_error = Self::compute_heading_error(heading, segment);

                // Filter by heading compatibility
                let heading_score = Self::heading_compatible(heading, segment);
                if heading_score > 1.5 {
                    // Heading incompatible (>135° off)
                    return None;
                }

                let confidence = Self::compute_confidence(
                    segment,
                    cross_track,
                    heading_error,
                    along_track,
                    self.max_search_radius_m,
                );

                let distance_to_endpoint = Self::distance_to_endpoint(along_track, segment);

                Some(MatchResult {
                    segment: segment.clone(),
                    cross_track_error: cross_track,
                    along_track_position: along_track,
                    heading_error,
                    confidence,
                    distance_to_endpoint,
                })
            })
            .collect();

        if scored_matches.is_empty() {
            // No compatible matches
            self.last_match = None;
            return None;
        }

        // Sort by confidence (descending)
        scored_matches.sort_by(|a, b| b.confidence.partial_cmp(&a.confidence).unwrap());

        let best_candidate = scored_matches.into_iter().next()?;

        // Apply hysteresis
        let selected_match = if let Some(ref last) = self.last_match {
            let time_since_last = timestamp - self.last_match_time;

            if time_since_last < self.match_hold_time_secs {
                // Within hold time: apply hysteresis
                if best_candidate.segment.id == last.segment.id {
                    // Same road: keep it
                    best_candidate
                } else if best_candidate.confidence > last.confidence + self.match_switch_threshold {
                    // New road significantly better: switch
                    best_candidate
                } else if speed > 20.0 {
                    // High speed: trust new match (highway merge/exit)
                    best_candidate
                } else {
                    // Keep old match (avoid jitter)
                    last.clone()
                }
            } else {
                // Hold time expired: use best candidate
                best_candidate
            }
        } else {
            // No previous match: use best candidate
            best_candidate
        };

        // Update state
        self.last_match = Some(selected_match.clone());
        self.last_match_time = timestamp;

        Some(selected_match)
    }

    /// Compute signed perpendicular distance and along-track position
    ///
    /// # Returns
    /// (cross_track_error, along_track_position)
    /// - cross_track_error: Signed distance in meters (+ right, - left)
    /// - along_track_position: Normalized [0, 1] projection parameter
    fn point_to_line_signed_distance(
        point: (f64, f64),
        segment: &RoadSegment,
    ) -> (f64, f64) {
        let coords: Vec<&Coord<f64>> = segment.geometry.coords().collect();

        if coords.len() < 2 {
            // Degenerate segment
            return (f64::INFINITY, 0.0);
        }

        // Use first and last point for simplified projection
        let p1 = coords[0];
        let p2 = coords[coords.len() - 1];

        // Convert to meters (approximate local cartesian)
        let lat_to_m = 111_000.0;
        let lon_to_m = 111_000.0 * point.0.to_radians().cos();

        let x0 = (point.1 - p1.x) * lon_to_m;
        let y0 = (point.0 - p1.y) * lat_to_m;

        let x1 = (p2.x - p1.x) * lon_to_m;
        let y1 = (p2.y - p1.y) * lat_to_m;

        let segment_length_sq = x1 * x1 + y1 * y1;

        if segment_length_sq < 1e-6 {
            // Degenerate segment (start == end)
            let dist = (x0 * x0 + y0 * y0).sqrt();
            return (dist, 0.0);
        }

        // Projection parameter t = (p0 - p1) · (p2 - p1) / |p2 - p1|²
        let t = (x0 * x1 + y0 * y1) / segment_length_sq;

        // Clamp to [0, 1] for perpendicular distance calculation
        let t_clamped = t.clamp(0.0, 1.0);

        // Closest point on segment
        let closest_x = x1 * t_clamped;
        let closest_y = y1 * t_clamped;

        // Cross-track error (signed via cross product)
        let cross_product = x0 * y1 - y0 * x1;
        let segment_length = segment_length_sq.sqrt();
        let signed_distance = cross_product / segment_length;

        // Along-track position (unclamped for endpoint detection)
        (signed_distance, t)
    }

    /// Compute heading error between vehicle and segment
    ///
    /// # Returns
    /// Angular difference in radians [-π, π]
    fn compute_heading_error(vehicle_heading: f64, segment: &RoadSegment) -> f64 {
        // Convert segment heading from degrees to radians
        let segment_heading_rad = segment.heading.to_radians();

        // Compute angular difference
        let mut diff = vehicle_heading - segment_heading_rad;

        // Normalize to [-π, π]
        while diff > std::f64::consts::PI {
            diff -= 2.0 * std::f64::consts::PI;
        }
        while diff < -std::f64::consts::PI {
            diff += 2.0 * std::f64::consts::PI;
        }

        // For bidirectional roads, consider reverse direction
        if !segment.one_way {
            let reverse_diff = diff + std::f64::consts::PI;
            let reverse_diff_normalized = if reverse_diff > std::f64::consts::PI {
                reverse_diff - 2.0 * std::f64::consts::PI
            } else {
                reverse_diff
            };

            // Return smaller error
            if reverse_diff_normalized.abs() < diff.abs() {
                return reverse_diff_normalized;
            }
        }

        diff
    }

    /// Check heading compatibility (filter before confidence scoring)
    ///
    /// # Returns
    /// Score where <1.0 = compatible, >1.5 = incompatible
    fn heading_compatible(vehicle_heading: f64, segment: &RoadSegment) -> f64 {
        let heading_error = Self::compute_heading_error(vehicle_heading, segment).abs();

        if segment.one_way {
            // One-way: strict within ±45°
            if heading_error > 0.785 {  // π/4
                return 2.0;  // Incompatible
            }
            heading_error / 0.785
        } else {
            // Bidirectional: allow ±90°
            if heading_error > std::f64::consts::FRAC_PI_2 {
                return 2.0;  // Incompatible
            }
            heading_error / std::f64::consts::FRAC_PI_2
        }
    }

    /// Compute composite confidence score [0, 1]
    ///
    /// # Components
    /// - Distance: 1 - (|cross_track| / max_radius)
    /// - Heading: 1 - (|heading_error| / π) * 0.7  (reduced weight)
    /// - Road class: segment.road_class.confidence_weight()
    /// - Along-track: 1.0 if [0, 1], else exponential decay
    fn compute_confidence(
        segment: &RoadSegment,
        cross_track: f64,
        heading_error: f64,
        along_track: f64,
        max_radius: f64,
    ) -> f64 {
        // Distance component [0, 1]
        let distance_score = (1.0 - cross_track.abs() / max_radius).max(0.0);

        // Heading component [0, 1] with reduced weight
        let heading_score = 1.0 - (heading_error.abs() / std::f64::consts::PI) * 0.7;

        // Road class weight
        let class_weight = segment.road_class.confidence_weight();

        // Along-track component (penalize off-segment positions)
        let along_track_score = if (0.0..=1.0).contains(&along_track) {
            1.0
        } else {
            // Exponential decay for off-segment
            let overshoot = if along_track < 0.0 {
                -along_track
            } else {
                along_track - 1.0
            };
            (-overshoot * 5.0).exp()
        };

        // Composite score (product of normalized components)
        distance_score * heading_score * class_weight * along_track_score
    }

    /// Distance to nearest segment endpoint (for transition detection)
    fn distance_to_endpoint(along_track: f64, segment: &RoadSegment) -> f64 {
        let coords: Vec<&Coord<f64>> = segment.geometry.coords().collect();
        if coords.len() < 2 {
            return 0.0;
        }

        // Approximate segment length in meters
        let p1 = coords[0];
        let p2 = coords[coords.len() - 1];

        let lat_diff = (p2.y - p1.y) * 111_000.0;
        let lon_diff = (p2.x - p1.x) * 111_000.0 * p1.y.to_radians().cos();
        let segment_length = (lat_diff * lat_diff + lon_diff * lon_diff).sqrt();

        // Distance to nearest endpoint
        let dist_to_start = along_track * segment_length;
        let dist_to_end = (1.0 - along_track) * segment_length;

        dist_to_start.min(dist_to_end)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::map_match::{RoadClass, RoadSegment};
    use geo::{Coord, LineString};

    fn make_test_segment(
        id: u64,
        coords: Vec<(f64, f64)>,
        heading: f64,
        one_way: bool,
    ) -> RoadSegment {
        let points: Vec<Coord<f64>> = coords
            .into_iter()
            .map(|(lon, lat)| Coord { x: lon, y: lat })
            .collect();

        RoadSegment {
            id,
            geometry: LineString::new(points),
            heading,
            road_class: RoadClass::Primary,
            one_way,
            name: Some("Test Road".to_string()),
        }
    }

    #[test]
    fn test_single_road_match() {
        // Create simple north-south road
        let segment = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,  // North
            false,
        );

        let tree = Arc::new(RoadTree::from_segments(vec![segment]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Vehicle on road, heading north
        let result = matcher.match_position(
            (37.7754, -122.4194),  // Midpoint
            0.0,                    // North
            10.0,                   // 10 m/s
            0.0,                    // t=0
        );

        assert!(result.is_some());
        let m = result.unwrap();
        assert_eq!(m.segment.id, 1);
        assert!(m.cross_track_error.abs() < 5.0, "Cross-track should be ~0");
        assert!(m.confidence > 0.8, "High confidence expected");
    }

    #[test]
    fn test_heading_compatibility() {
        // East-west road (heading 90°)
        let segment = make_test_segment(
            1,
            vec![(-122.4200, 37.7749), (-122.4190, 37.7749)],
            90.0,
            false,  // Bidirectional
        );

        let tree = Arc::new(RoadTree::from_segments(vec![segment]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Vehicle heading east (90° = π/2 rad)
        let result = matcher.match_position(
            (37.7749, -122.4195),
            std::f64::consts::FRAC_PI_2,
            15.0,
            0.0,
        );

        assert!(result.is_some());

        // Vehicle heading west (270° = 3π/2 rad) - should match bidirectional road
        let result = matcher.match_position(
            (37.7749, -122.4195),
            3.0 * std::f64::consts::FRAC_PI_2,
            15.0,
            1.0,
        );

        assert!(result.is_some(), "Bidirectional road should match reverse heading");
    }

    #[test]
    fn test_one_way_heading_filter() {
        // One-way road heading east
        let segment = make_test_segment(
            1,
            vec![(-122.4200, 37.7749), (-122.4190, 37.7749)],
            90.0,
            true,  // One-way
        );

        let tree = Arc::new(RoadTree::from_segments(vec![segment]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Vehicle heading west (opposite direction) - should reject
        let result = matcher.match_position(
            (37.7749, -122.4195),
            3.0 * std::f64::consts::FRAC_PI_2,  // 270° west
            15.0,
            0.0,
        );

        assert!(result.is_none(), "One-way road should reject reverse heading");
    }

    #[test]
    fn test_hysteresis_hold_time() {
        // Two parallel roads
        let seg1 = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );
        let seg2 = make_test_segment(
            2,
            vec![(-122.4196, 37.7749), (-122.4196, 37.7759)],
            0.0,
            false,
        );

        let tree = Arc::new(RoadTree::from_segments(vec![seg1, seg2]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Initial match to segment 1
        let result = matcher.match_position(
            (37.7754, -122.4194),
            0.0,
            10.0,
            0.0,
        );
        assert_eq!(result.unwrap().segment.id, 1);

        // Move slightly toward segment 2 at t=1s (within hold time)
        let result = matcher.match_position(
            (37.7754, -122.4195),  // Closer to seg2
            0.0,
            10.0,
            1.0,
        );
        assert_eq!(result.unwrap().segment.id, 1, "Should hold previous match");

        // Move at t=4s (hold time expired)
        let result = matcher.match_position(
            (37.7754, -122.4196),  // On seg2
            0.0,
            10.0,
            4.0,
        );
        assert_eq!(result.unwrap().segment.id, 2, "Should switch after hold time");
    }

    #[test]
    fn test_hysteresis_confidence_threshold() {
        let seg1 = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );
        let seg2 = make_test_segment(
            2,
            vec![(-122.4210, 37.7749), (-122.4210, 37.7759)],
            0.0,
            false,
        );

        let tree = Arc::new(RoadTree::from_segments(vec![seg1, seg2]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Match to segment 1
        matcher.match_position((37.7754, -122.4194), 0.0, 10.0, 0.0);

        // Jump to segment 2 (far away, high confidence difference)
        let result = matcher.match_position(
            (37.7754, -122.4210),
            0.0,
            10.0,
            1.0,
        );
        assert_eq!(result.unwrap().segment.id, 2, "Should switch on large confidence delta");
    }

    #[test]
    fn test_cross_track_error() {
        let segment = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );

        // Point 10m east (positive cross-track)
        let (cross_track, _) = MapMatcher::point_to_line_signed_distance(
            (37.7754, -122.4193),
            &segment,
        );
        assert!(cross_track > 0.0, "East of road should be positive");

        // Point 10m west (negative cross-track)
        let (cross_track, _) = MapMatcher::point_to_line_signed_distance(
            (37.7754, -122.4195),
            &segment,
        );
        assert!(cross_track < 0.0, "West of road should be negative");
    }

    #[test]
    fn test_along_track_position() {
        let segment = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );

        // Point at start
        let (_, along) = MapMatcher::point_to_line_signed_distance(
            (37.7749, -122.4194),
            &segment,
        );
        assert!(along < 0.1, "Start should be ~0");

        // Point at midpoint
        let (_, along) = MapMatcher::point_to_line_signed_distance(
            (37.7754, -122.4194),
            &segment,
        );
        assert!((along - 0.5).abs() < 0.1, "Midpoint should be ~0.5");

        // Point at end
        let (_, along) = MapMatcher::point_to_line_signed_distance(
            (37.7759, -122.4194),
            &segment,
        );
        assert!(along > 0.9, "End should be ~1.0");
    }

    #[test]
    fn test_no_nearby_roads() {
        let segment = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );

        let tree = Arc::new(RoadTree::from_segments(vec![segment]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Query 1km away
        let result = matcher.match_position(
            (37.7849, -122.4194),
            0.0,
            10.0,
            0.0,
        );

        assert!(result.is_none(), "Should return None when no nearby roads");
    }

    #[test]
    fn test_high_speed_override() {
        let seg1 = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );
        let seg2 = make_test_segment(
            2,
            vec![(-122.4196, 37.7749), (-122.4196, 37.7759)],
            0.0,
            false,
        );

        let tree = Arc::new(RoadTree::from_segments(vec![seg1, seg2]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // Match to segment 1
        matcher.match_position((37.7754, -122.4194), 0.0, 10.0, 0.0);

        // Move to segment 2 at high speed (highway merge)
        let result = matcher.match_position(
            (37.7754, -122.4196),
            0.0,
            25.0,  // >20 m/s
            1.0,
        );

        assert_eq!(result.unwrap().segment.id, 2, "High speed should override hysteresis");
    }

    #[test]
    fn test_gps_spike_rejection() {
        let segment = make_test_segment(
            1,
            vec![(-122.4194, 37.7749), (-122.4194, 37.7759)],
            0.0,
            false,
        );

        let tree = Arc::new(RoadTree::from_segments(vec![segment]));
        let mut matcher = MapMatcher::new(tree, 30.0);

        // GPS spike (>50 m/s)
        let result = matcher.match_position(
            (37.7754, -122.4194),
            0.0,
            55.0,  // Invalid speed
            0.0,
        );

        assert!(result.is_none(), "Should reject GPS spikes");
    }
}
