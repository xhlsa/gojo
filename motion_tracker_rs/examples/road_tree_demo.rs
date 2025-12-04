/// Example: Using RoadTree for spatial indexing
///
/// Demonstrates Phase 5 integration with TileManager
/// Run with: cargo run --example road_tree_demo
use motion_tracker_rs::map_match::{RoadClass, RoadSegment, RoadTree};
use geo::{Coord, LineString};

fn main() {
    println!("Road Tree Spatial Index Demo\n");

    // Create sample road segments (normally from TileManager)
    let segments = vec![
        make_segment(1, "Main St", vec![
            (-122.4194, 37.7749),
            (-122.4195, 37.7750),
        ]),
        make_segment(2, "Market St", vec![
            (-122.4200, 37.7760),
            (-122.4210, 37.7770),
        ]),
        make_segment(3, "Mission St", vec![
            (-122.4180, 37.7730),
            (-122.4185, 37.7735),
        ]),
        make_segment(4, "Highway 101", vec![
            (-122.4300, 37.7800),
            (-122.4310, 37.7810),
        ]),
    ];

    println!("Building R-Tree from {} road segments...", segments.len());
    let tree = RoadTree::from_segments(segments);
    println!("Tree contains {} segments\n", tree.segment_count());

    // Example 1: Find roads near a GPS position (37.7749, -122.4194)
    let query_pos = (37.7749, -122.4194);
    let radius_m = 100.0;

    println!("Query 1: Roads within {}m of ({:.4}, {:.4})",
             radius_m, query_pos.0, query_pos.1);
    let candidates = tree.nearest_segments(query_pos, radius_m);
    println!("Found {} candidate road(s):", candidates.len());
    for (i, seg) in candidates.iter().enumerate() {
        println!("  {}. ID={} Name={:?}",
                 i + 1, seg.id, seg.name.as_deref().unwrap_or("Unnamed"));
    }
    println!();

    // Example 2: Find roads in a bounding box (tile region)
    println!("Query 2: Roads in bounding box [37.773, -122.422] to [37.778, -122.418]");
    let bbox_results = tree.segments_in_bbox(37.773, -122.422, 37.778, -122.418);
    println!("Found {} segment(s) in bbox:", bbox_results.len());
    for (i, seg) in bbox_results.iter().enumerate() {
        println!("  {}. ID={} Name={:?}",
                 i + 1, seg.id, seg.name.as_deref().unwrap_or("Unnamed"));
    }
    println!();

    // Example 3: Larger radius to show distance sorting
    let large_radius_m = 5000.0;
    println!("Query 3: Roads within {}m of ({:.4}, {:.4})",
             large_radius_m, query_pos.0, query_pos.1);
    let all_candidates = tree.nearest_segments(query_pos, large_radius_m);
    println!("Found {} roads (sorted by distance):", all_candidates.len());
    for (i, seg) in all_candidates.iter().enumerate() {
        println!("  {}. ID={} Name={:?} Class={:?}",
                 i + 1, seg.id, seg.name.as_deref().unwrap_or("Unnamed"), seg.road_class);
    }

    println!("\nPhase 5 Complete: R-Tree spatial index operational!");
    println!("Ready for Phase 6: Map matcher integration");
}

fn make_segment(id: u64, name: &str, coords: Vec<(f64, f64)>) -> RoadSegment {
    let points: Vec<Coord<f64>> = coords
        .into_iter()
        .map(|(lon, lat)| Coord { x: lon, y: lat })
        .collect();

    RoadSegment {
        id,
        geometry: LineString::new(points),
        heading: 0.0,
        road_class: RoadClass::Primary,
        one_way: false,
        name: Some(name.to_string()),
    }
}
