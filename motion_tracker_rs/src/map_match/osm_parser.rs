use geo::{LineString, Point};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RoadClass {
    Motorway,
    Primary,
    Secondary,
    Residential,
    Service,
    Unknown,
}

impl RoadClass {
    /// Lane width in meters for map-matching confidence weighting
    pub fn lane_width(&self) -> f64 {
        match self {
            RoadClass::Motorway => 3.7,    // Wide highway lanes
            RoadClass::Primary => 3.5,      // Major arterial
            RoadClass::Secondary => 3.2,    // Secondary arterial
            RoadClass::Residential => 3.0,  // Residential street
            RoadClass::Service => 2.5,      // Service road, parking lot
            RoadClass::Unknown => 3.0,      // Default assumption
        }
    }

    /// Confidence weight for map-matching (higher = prefer this road class)
    pub fn confidence_weight(&self) -> f64 {
        match self {
            RoadClass::Motorway => 1.5,     // Prefer highways when high speed
            RoadClass::Primary => 1.2,
            RoadClass::Secondary => 1.0,
            RoadClass::Residential => 0.8,
            RoadClass::Service => 0.5,      // Deprioritize parking lots
            RoadClass::Unknown => 0.7,
        }
    }

    /// Parse from OSM highway tag
    fn from_highway_tag(tag: &str) -> Self {
        match tag {
            "motorway" | "motorway_link" | "trunk" | "trunk_link" => RoadClass::Motorway,
            "primary" | "primary_link" => RoadClass::Primary,
            "secondary" | "secondary_link" | "tertiary" | "tertiary_link" => RoadClass::Secondary,
            "residential" | "living_street" => RoadClass::Residential,
            "service" | "parking_aisle" => RoadClass::Service,
            _ => RoadClass::Unknown,
        }
    }
}

#[derive(Clone, Debug)]
pub struct RoadSegment {
    pub id: u64,
    pub geometry: LineString<f64>,
    pub heading: f64,
    pub road_class: RoadClass,
    pub one_way: bool,
    pub name: Option<String>,
}

impl RoadSegment {
    /// Calculate bearing from start to end point using geodetic formula
    pub fn calculate_heading(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
        let lat1_rad = lat1.to_radians();
        let lat2_rad = lat2.to_radians();
        let delta_lon = (lon2 - lon1).to_radians();

        let y = delta_lon.sin() * lat2_rad.cos();
        let x = lat1_rad.cos() * lat2_rad.sin()
              - lat1_rad.sin() * lat2_rad.cos() * delta_lon.cos();

        let bearing_rad = y.atan2(x);

        // Convert to degrees [0, 360)
        let bearing_deg = bearing_rad.to_degrees();
        (bearing_deg + 360.0) % 360.0
    }
}

// OSM JSON deserialization structures
#[derive(Debug, Deserialize)]
struct OsmResponse {
    elements: Vec<OsmElement>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type")]
enum OsmElement {
    #[serde(rename = "node")]
    Node {
        id: u64,
        lat: f64,
        lon: f64,
    },
    #[serde(rename = "way")]
    Way {
        id: u64,
        nodes: Vec<u64>,
        #[serde(default)]
        tags: HashMap<String, String>,
    },
    #[serde(other)]
    Other,
}

/// Parse OSM JSON response into RoadSegment structures
pub fn parse_osm_json(json: &str) -> Result<Vec<RoadSegment>, String> {
    // Deserialize JSON
    let response: OsmResponse = serde_json::from_str(json)
        .map_err(|e| format!("Failed to parse OSM JSON: {}", e))?;

    // Build node lookup table
    let mut nodes: HashMap<u64, (f64, f64)> = HashMap::new();
    for element in &response.elements {
        if let OsmElement::Node { id, lat, lon } = element {
            nodes.insert(*id, (*lat, *lon));
        }
    }

    // Process ways into road segments
    let mut segments = Vec::new();

    for element in &response.elements {
        if let OsmElement::Way { id, nodes: node_ids, tags } = element {
            // Filter: only process ways with highway tag
            let highway = match tags.get("highway") {
                Some(h) => h,
                None => continue,
            };

            let road_class = RoadClass::from_highway_tag(highway);
            let one_way = tags.get("oneway").map(|s| s.as_str()) == Some("yes");
            let name = tags.get("name").cloned();

            // Build LineString geometry from node IDs
            let mut coords = Vec::new();
            let mut has_missing_nodes = false;
            for node_id in node_ids {
                match nodes.get(node_id) {
                    Some(&(lat, lon)) => {
                        coords.push((lon, lat)); // geo crate uses (x, y) = (lon, lat)
                    }
                    None => {
                        // Way has missing node(s) - skip entire way to avoid broken geometry
                        eprintln!("Warning: Way {} references missing node {}", id, node_id);
                        has_missing_nodes = true;
                        break;
                    }
                }
            }

            // Skip way if it has missing nodes or insufficient points
            if has_missing_nodes || coords.len() < 2 {
                continue;
            }

            // Calculate heading from first to last point
            let (lon1, lat1) = coords[0];
            let (lon2, lat2) = coords[coords.len() - 1];
            let heading = RoadSegment::calculate_heading(lat1, lon1, lat2, lon2);

            // Create LineString geometry
            let points: Vec<Point<f64>> = coords.into_iter()
                .map(|(lon, lat)| Point::new(lon, lat))
                .collect();
            let geometry = LineString::new(points.into_iter().map(|p| p.0).collect());

            segments.push(RoadSegment {
                id: *id,
                geometry,
                heading,
                road_class,
                one_way,
                name,
            });
        }
    }

    if segments.is_empty() {
        return Err("No valid road segments found in OSM data".to_string());
    }

    Ok(segments)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_heading_calculation() {
        // North
        let h = RoadSegment::calculate_heading(0.0, 0.0, 1.0, 0.0);
        assert!((h - 0.0).abs() < 1.0);

        // East
        let h = RoadSegment::calculate_heading(0.0, 0.0, 0.0, 1.0);
        assert!((h - 90.0).abs() < 1.0);

        // South
        let h = RoadSegment::calculate_heading(1.0, 0.0, 0.0, 0.0);
        assert!((h - 180.0).abs() < 1.0);

        // West
        let h = RoadSegment::calculate_heading(0.0, 1.0, 0.0, 0.0);
        assert!((h - 270.0).abs() < 1.0);
    }

    #[test]
    fn test_road_class_from_tag() {
        assert_eq!(RoadClass::from_highway_tag("motorway"), RoadClass::Motorway);
        assert_eq!(RoadClass::from_highway_tag("primary_link"), RoadClass::Primary);
        assert_eq!(RoadClass::from_highway_tag("residential"), RoadClass::Residential);
        assert_eq!(RoadClass::from_highway_tag("service"), RoadClass::Service);
        assert_eq!(RoadClass::from_highway_tag("footway"), RoadClass::Unknown);
    }

    #[test]
    fn test_parse_minimal_osm() {
        let json = r#"{
            "elements": [
                {"type": "node", "id": 1, "lat": 37.7749, "lon": -122.4194},
                {"type": "node", "id": 2, "lat": 37.7750, "lon": -122.4195},
                {
                    "type": "way",
                    "id": 100,
                    "nodes": [1, 2],
                    "tags": {
                        "highway": "residential",
                        "name": "Main Street"
                    }
                }
            ]
        }"#;

        let result = parse_osm_json(json);
        assert!(result.is_ok());

        let segments = result.unwrap();
        assert_eq!(segments.len(), 1);
        assert_eq!(segments[0].id, 100);
        assert_eq!(segments[0].road_class, RoadClass::Residential);
        assert_eq!(segments[0].name, Some("Main Street".to_string()));
        assert_eq!(segments[0].one_way, false);
        assert_eq!(segments[0].geometry.0.len(), 2);
    }

    #[test]
    fn test_parse_missing_nodes() {
        let json = r#"{
            "elements": [
                {"type": "node", "id": 1, "lat": 37.7749, "lon": -122.4194},
                {
                    "type": "way",
                    "id": 100,
                    "nodes": [1, 999],
                    "tags": {"highway": "primary"}
                }
            ]
        }"#;

        let result = parse_osm_json(json);
        // Should return error since no valid segments
        assert!(result.is_err());
    }

    #[test]
    fn test_parse_oneway() {
        let json = r#"{
            "elements": [
                {"type": "node", "id": 1, "lat": 37.7749, "lon": -122.4194},
                {"type": "node", "id": 2, "lat": 37.7750, "lon": -122.4195},
                {
                    "type": "way",
                    "id": 100,
                    "nodes": [1, 2],
                    "tags": {
                        "highway": "motorway",
                        "oneway": "yes"
                    }
                }
            ]
        }"#;

        let segments = parse_osm_json(json).unwrap();
        assert_eq!(segments[0].one_way, true);
        assert_eq!(segments[0].road_class, RoadClass::Motorway);
    }

    #[test]
    fn test_parse_partial_missing_nodes() {
        // Test case: way with nodes [1, 2, 999, 3] where 999 is missing
        // This should be rejected entirely to avoid broken geometry
        let json = r#"{
            "elements": [
                {"type": "node", "id": 1, "lat": 37.7749, "lon": -122.4194},
                {"type": "node", "id": 2, "lat": 37.7750, "lon": -122.4195},
                {"type": "node", "id": 3, "lat": 37.7751, "lon": -122.4196},
                {
                    "type": "way",
                    "id": 100,
                    "nodes": [1, 2, 999, 3],
                    "tags": {"highway": "primary"}
                }
            ]
        }"#;

        let result = parse_osm_json(json);
        // Should return error since the only way has missing nodes
        assert!(result.is_err(), "Way with partial missing nodes should be rejected");
    }
}
