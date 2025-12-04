/// Example: Fetch OSM data from Overpass API and populate TileManager cache
///
/// Usage:
///   cargo run --example overpass_example
///
/// This demonstrates:
/// - Creating OverpassFetcher
/// - Fetching tiles from Overpass API
/// - Populating TileManager cache
/// - Saving tiles to disk for offline use

use motion_tracker_rs::map_match::{OverpassFetcher, TileManager};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize logging
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    // San Francisco coordinates
    let lat = 37.7749;
    let lon = -122.4194;
    let zoom = 14;

    // Create cache directory
    let cache_dir = PathBuf::from("/sdcard/gojo/map_cache");
    std::fs::create_dir_all(&cache_dir)?;

    // Initialize TileManager
    let mut manager = TileManager::new(cache_dir, lat, lon, zoom);
    log::info!("Initialized TileManager at ({}, {}), zoom {}", lat, lon, zoom);

    // Create Overpass fetcher
    let mut fetcher = OverpassFetcher::new();

    // Get current tile and neighbors (3x3 grid = 9 tiles)
    let current_tile = manager.current_tile();
    let mut tiles_to_fetch = vec![current_tile];
    tiles_to_fetch.extend_from_slice(&current_tile.neighbors());

    log::info!("Fetching {} tiles from Overpass API...", tiles_to_fetch.len());

    // Fetch tiles one by one (respects rate limiting)
    let mut success_count = 0;
    let mut error_count = 0;

    for (i, tile) in tiles_to_fetch.iter().enumerate() {
        log::info!("Fetching tile {}/{}: {}", i + 1, tiles_to_fetch.len(), tile);

        match fetcher.fetch_tile(*tile).await {
            Ok(segments) => {
                log::info!("  ✓ Fetched {} road segments", segments.len());

                // Insert into TileManager
                manager.insert_tile(*tile, segments.clone());

                // Save to disk for offline use
                if let Err(e) = manager.save_tile_to_disk(*tile, &segments) {
                    log::warn!("  Failed to save tile to disk: {}", e);
                } else {
                    log::info!("  ✓ Saved to disk cache");
                }

                success_count += 1;
            }
            Err(e) => {
                log::error!("  ✗ Failed to fetch tile: {}", e);
                error_count += 1;

                // Insert empty tile to avoid retrying
                manager.insert_tile(*tile, Vec::new());
            }
        }
    }

    // Summary
    log::info!("\n=== Fetch Summary ===");
    log::info!("Success: {}", success_count);
    log::info!("Errors:  {}", error_count);
    log::info!("Total:   {}", tiles_to_fetch.len());
    log::info!("Active tiles in memory: {}", manager.tile_count());

    Ok(())
}
