# Map Matching Module - Phase 4: Overpass API Fetcher

## Overview

The Overpass API fetcher provides async HTTP client functionality to fetch OpenStreetMap road data from the Overpass API and populate the TileManager cache.

## Architecture

```
┌─────────────────┐
│  OverpassAPI    │  (external service)
└────────┬────────┘
         │ HTTP POST
         ▼
┌─────────────────┐
│ OverpassFetcher │  (this module)
│  - Rate limit   │
│  - Retry logic  │
│  - Error handle │
└────────┬────────┘
         │ Vec<RoadSegment>
         ▼
┌─────────────────┐
│  TileManager    │  (Phase 3)
│  - In-memory    │
│  - Disk cache   │
└─────────────────┘
```

## Components Implemented

### 1. OverpassFetcher Struct

Main async HTTP client for Overpass API:

- **HTTP Client**: reqwest with 30-second timeout
- **Base URL**: https://overpass-api.de/api/interpreter
- **User-Agent**: Gojo Motion Tracker/0.1.0
- **Rate Limiting**: 1-second minimum interval between requests

### 2. FetchError Enum

Comprehensive error handling:

- `NetworkTimeout` - Request timed out
- `HttpError(u16)` - HTTP error with status code
- `OverpassTimeout` - API query timeout
- `OverpassBlockByDefault` - Query blocked by API
- `RateLimited` - Too many requests
- `ParseError(String)` - Failed to parse OSM JSON
- `NoData` - Empty response
- `UnknownError(String)` - Unexpected error

### 3. Core Methods

**new() -> Self**
- Initializes reqwest Client with 30s timeout
- Sets up rate limiting (1-second minimum)
- Configures User-Agent header

**build_query(&self, tile: &TileCoord) -> String**
- Generates Overpass QL query for tile bbox
- Filters highways: motorway, trunk, primary, secondary, tertiary, residential, service
- Sets 30-second query timeout

**fetch_tile(&mut self, tile: TileCoord) -> Result<Vec<RoadSegment>, FetchError>**
- Respects rate limit (sleeps if needed)
- POSTs query to Overpass API
- Handles HTTP errors and timeouts
- Parses OSM JSON response
- Returns road segments on success

### 4. Retry Logic

Smart error recovery:

- **HTTP 429**: Sleep 60 seconds, retry
- **Overpass timeout**: Exponential backoff (2s, 4s, 8s), max 3 retries
- **Network timeout**: Return error (transient)
- **Parse error**: Return error (don't crash)

### 5. Query Format

Example Overpass QL query for San Francisco tile:

```text
[out:json][timeout:30];
(
  way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service"]
    (37.77,-122.42,37.78,-122.41);
);
out body;
>;
out skel qt;
```

**Query Explanation**:
- `[out:json]` - Return JSON format
- `[timeout:30]` - 30-second query timeout
- `way["highway"~"..."]` - Filter by highway tag regex
- `(south,west,north,east)` - Bounding box from tile coords
- `out body` - Return way tags and node IDs
- `>` - Include all referenced nodes
- `out skel qt` - Return node coordinates (skeleton)

## Usage Example

```rust
use motion_tracker_rs::map_match::{OverpassFetcher, TileManager};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Initialize components
    let mut fetcher = OverpassFetcher::new();
    let mut manager = TileManager::new(
        PathBuf::from("/sdcard/gojo/map_cache"),
        37.7749,
        -122.4194,
        14
    );

    // Fetch current tile + 8 neighbors
    let current_tile = manager.current_tile();
    let mut tiles = vec![current_tile];
    tiles.extend_from_slice(&current_tile.neighbors());

    // Fetch and cache tiles
    for tile in tiles {
        match fetcher.fetch_tile(tile).await {
            Ok(segments) => {
                println!("Fetched {} road segments for {}", segments.len(), tile);
                manager.insert_tile(tile, segments.clone());
                manager.save_tile_to_disk(tile, &segments)?;
            }
            Err(e) => {
                eprintln!("Failed to fetch {}: {}", tile, e);
                manager.insert_tile(tile, Vec::new()); // Mark as attempted
            }
        }
    }

    Ok(())
}
```

See `examples/overpass_example.rs` for a complete working example.

## Testing

### Unit Tests

Run with: `cargo test --lib map_match::overpass_fetcher`

**Test Coverage**:
- ✅ `test_build_query` - Verify Overpass QL syntax
- ✅ `test_fetch_error_display` - Error message formatting
- ✅ `test_rate_limit_tracking` - Rate limit state tracking
- ✅ `test_bbox_coordinates_in_query` - Bounding box in query
- ✅ `test_respect_rate_limit_sleep` - Sleep timing verification

### Integration Tests

Disabled by default (require network):

- `test_fetch_tile_integration` - Fetch real SF tile
- `test_fetch_empty_tile` - Fetch ocean tile (no roads)

Run with: `cargo test --lib map_match::overpass_fetcher -- --ignored`

## Performance

**Rate Limiting**:
- 1 request per second (Overpass API limit)
- 9 tiles (3x3 grid) = ~9-10 seconds to fetch
- Cached tiles avoid repeat fetches

**Data Size**:
- Typical SF tile: 100-500 road segments
- JSON response: ~50-200 KB
- Compressed cache: ~10-50 KB (gzip)

**Timeouts**:
- Network: 30 seconds
- Overpass query: 30 seconds
- Retry backoff: 2s, 4s, 8s (exponential)

## Dependencies

Added to `Cargo.toml`:

```toml
[dependencies]
reqwest = { version = "0.11", features = ["json"] }
log = "0.4"

[dev-dependencies]
env_logger = "0.11"
```

## API Politeness

Following Overpass API best practices:

- ✅ User-Agent header identifies our app
- ✅ 1-second minimum between requests
- ✅ Respects 429 rate limit responses
- ✅ Handles timeout errors gracefully
- ✅ Caches results to minimize repeat requests

## Error Recovery

**Transient Errors** (retry):
- HTTP 429 (rate limited)
- Overpass timeout (up to 3 attempts)

**Permanent Errors** (fail fast):
- Network timeout
- HTTP 4xx/5xx (except 429)
- Parse errors
- No data returned

## Next Steps (Phase 5+)

1. **Background Fetcher**: Async task that pre-fetches tiles ahead of vehicle position
2. **Priority Queue**: Fetch current tile first, then neighbors
3. **Batch Queries**: Combine multiple tiles into single Overpass query (advanced)
4. **Fallback Server**: Use alternative Overpass mirrors if primary fails
5. **Cache Expiry**: Refresh tiles older than N days (OSM data changes)

## Files Modified

- **New**: `src/map_match/overpass_fetcher.rs` (297 lines)
- **Updated**: `src/map_match/mod.rs` (expose OverpassFetcher)
- **Updated**: `Cargo.toml` (add reqwest, log)
- **New**: `examples/overpass_example.rs` (demo usage)

## Status

✅ Phase 4 Complete
- All unit tests passing (5/5)
- Zero clippy warnings
- Ready for integration with TileManager event loop
- Example demonstrating full workflow
