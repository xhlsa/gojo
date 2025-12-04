use super::{parse_osm_json, RoadSegment, TileCoord};
use std::fmt::{Display, Formatter};
use std::time::{Duration, Instant};

/// Fetch errors from Overpass API
#[derive(Debug, Clone)]
pub enum FetchError {
    NetworkTimeout,
    HttpError(u16),
    OverpassTimeout,
    OverpassBlockByDefault,
    RateLimited,
    ParseError(String),
    NoData,
    UnknownError(String),
}

impl Display for FetchError {
    fn fmt(&self, f: &mut Formatter) -> std::fmt::Result {
        match self {
            FetchError::NetworkTimeout => write!(f, "Network timeout"),
            FetchError::HttpError(code) => write!(f, "HTTP error: {}", code),
            FetchError::OverpassTimeout => write!(f, "Overpass API timeout"),
            FetchError::OverpassBlockByDefault => write!(f, "Query blocked by Overpass"),
            FetchError::RateLimited => write!(f, "Rate limited by Overpass API"),
            FetchError::ParseError(msg) => write!(f, "Parse error: {}", msg),
            FetchError::NoData => write!(f, "No data returned"),
            FetchError::UnknownError(msg) => write!(f, "Unknown error: {}", msg),
        }
    }
}

/// Rate limiter for Overpass API requests
struct RateLimit {
    last_request: Instant,
    min_interval_secs: u64,
}

impl RateLimit {
    fn new(min_interval_secs: u64) -> Self {
        RateLimit {
            last_request: Instant::now() - Duration::from_secs(min_interval_secs),
            min_interval_secs,
        }
    }
}

/// Overpass API client for fetching OSM road data
///
/// # Rate Limiting
/// - Minimum 1 second between requests
/// - Automatic backoff for timeout errors
/// - HTTP 429 triggers 60-second sleep
///
/// # Query Strategy
/// - Filters highways: motorway, trunk, primary, secondary, tertiary, residential, service
/// - 30-second timeout per query
/// - Returns JSON with coordinates and tags
///
/// # Error Handling
/// - Network timeout: transient, retry with delay
/// - Overpass timeout: retry up to 3 times with exponential backoff
/// - HTTP 429: sleep 60 seconds before retry
/// - Parse error: log but don't crash
pub struct OverpassFetcher {
    client: reqwest::Client,
    base_url: String,
    timeout_secs: u64,
    rate_limit: RateLimit,
}

impl OverpassFetcher {
    /// Create new Overpass fetcher with default settings
    pub fn new() -> Self {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent("Gojo Motion Tracker/0.1.0 (https://github.com/yourusername/gojo)")
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());

        OverpassFetcher {
            client,
            base_url: "https://overpass-api.de/api/interpreter".to_string(),
            timeout_secs: 30,
            rate_limit: RateLimit::new(1),
        }
    }

    /// Build Overpass QL query for tile bounding box
    ///
    /// # Query Format
    /// ```text
    /// [out:json][timeout:30];
    /// (
    ///   way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service"]
    ///     (south,west,north,east);
    /// );
    /// out body;
    /// >;
    /// out skel qt;
    /// ```
    fn build_query(&self, tile: &TileCoord) -> String {
        let (min_lat, min_lon, max_lat, max_lon) = tile.bbox();

        format!(
            "[out:json][timeout:{}];\n\
            (\n  \
              way[\"highway\"~\"motorway|trunk|primary|secondary|tertiary|residential|service\"]\
              ({},{},{},{});\n\
            );\n\
            out body;\n\
            >;\n\
            out skel qt;",
            self.timeout_secs,
            min_lat,
            min_lon,
            max_lat,
            max_lon
        )
    }

    /// Respect rate limit by sleeping if needed
    async fn respect_rate_limit(&mut self) {
        let elapsed = self.rate_limit.last_request.elapsed().as_secs();
        if elapsed < self.rate_limit.min_interval_secs {
            let sleep_time = self.rate_limit.min_interval_secs - elapsed;
            tokio::time::sleep(Duration::from_secs(sleep_time)).await;
        }
        self.rate_limit.last_request = Instant::now();
    }

    /// Fetch road segments for a tile from Overpass API
    ///
    /// # Returns
    /// - Ok(Vec<RoadSegment>) on success
    /// - Err(FetchError) on failure
    ///
    /// # Retry Logic
    /// - Overpass timeout: retry up to 3 times with exponential backoff (2s, 4s, 8s)
    /// - HTTP 429: sleep 60 seconds, then retry
    /// - Network timeout: treat as transient, return error
    pub async fn fetch_tile(&mut self, tile: TileCoord) -> Result<Vec<RoadSegment>, FetchError> {
        const MAX_RETRIES: u32 = 3;

        for attempt in 0..MAX_RETRIES {
            // Respect rate limit
            self.respect_rate_limit().await;

            // Build query
            let query = self.build_query(&tile);

            // Execute HTTP POST
            let response = match self.client
                .post(&self.base_url)
                .body(query)
                .send()
                .await
            {
                Ok(resp) => resp,
                Err(e) => {
                    if e.is_timeout() {
                        return Err(FetchError::NetworkTimeout);
                    }
                    return Err(FetchError::UnknownError(e.to_string()));
                }
            };

            // Check HTTP status
            let status = response.status();
            if status == 429 {
                // Rate limited - sleep 60 seconds
                log::warn!("Rate limited by Overpass API, sleeping 60 seconds");
                tokio::time::sleep(Duration::from_secs(60)).await;
                continue;
            } else if !status.is_success() {
                return Err(FetchError::HttpError(status.as_u16()));
            }

            // Get response body
            let body = match response.text().await {
                Ok(text) => text,
                Err(e) => {
                    return Err(FetchError::UnknownError(format!("Failed to read response: {}", e)));
                }
            };

            // Check for Overpass-specific errors
            if body.contains("error") {
                if body.contains("timeout") {
                    // Overpass query timeout - retry with exponential backoff
                    let backoff = 2u64.pow(attempt);
                    log::warn!(
                        "Overpass timeout on attempt {}/{}, retrying in {}s",
                        attempt + 1,
                        MAX_RETRIES,
                        backoff
                    );
                    tokio::time::sleep(Duration::from_secs(backoff)).await;
                    continue;
                } else if body.contains("blocked") || body.contains("rate") {
                    return Err(FetchError::OverpassBlockByDefault);
                }
            }

            // Parse OSM JSON
            match parse_osm_json(&body) {
                Ok(segments) => {
                    if segments.is_empty() {
                        return Err(FetchError::NoData);
                    }
                    return Ok(segments);
                }
                Err(e) => {
                    return Err(FetchError::ParseError(e));
                }
            }
        }

        // Exhausted retries
        Err(FetchError::OverpassTimeout)
    }
}

impl Default for OverpassFetcher {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_query() {
        let fetcher = OverpassFetcher::new();
        let tile = TileCoord::from_latlon(37.7749, -122.4194, 14);

        let query = fetcher.build_query(&tile);

        // Verify query contains essential components
        assert!(query.contains("[out:json]"));
        assert!(query.contains("[timeout:30]"));
        assert!(query.contains("way[\"highway\""));
        assert!(query.contains("motorway"));
        assert!(query.contains("residential"));
        assert!(query.contains("out body"));
        assert!(query.contains("out skel qt"));

        // Verify bounding box format (should have 4 coordinates)
        let bbox_count = query.matches(',').count();
        assert!(bbox_count >= 3, "Query should contain lat,lon,lat,lon bbox");
    }

    #[test]
    fn test_rate_limit_tracking() {
        let mut rate_limit = RateLimit::new(1);

        // First call should be immediate (already past min_interval in constructor)
        let first_elapsed = rate_limit.last_request.elapsed().as_secs();
        assert!(first_elapsed >= 1);

        // Update timestamp
        rate_limit.last_request = Instant::now();

        // Second call immediately after should require waiting
        let second_elapsed = rate_limit.last_request.elapsed().as_secs();
        assert!(second_elapsed < 1);
    }

    #[test]
    fn test_fetch_error_display() {
        let errors = vec![
            FetchError::NetworkTimeout,
            FetchError::HttpError(404),
            FetchError::OverpassTimeout,
            FetchError::OverpassBlockByDefault,
            FetchError::RateLimited,
            FetchError::ParseError("test".to_string()),
            FetchError::NoData,
            FetchError::UnknownError("unknown".to_string()),
        ];

        for err in errors {
            let display = format!("{}", err);
            assert!(!display.is_empty());
        }
    }

    #[tokio::test]
    async fn test_respect_rate_limit_sleep() {
        let mut fetcher = OverpassFetcher::new();

        // Set last request to just now
        fetcher.rate_limit.last_request = Instant::now();

        // This should sleep for ~1 second
        let start = Instant::now();
        fetcher.respect_rate_limit().await;
        let elapsed = start.elapsed().as_millis();

        // Should have slept close to 1 second (allow 100ms tolerance)
        assert!(elapsed >= 900 && elapsed <= 1100);
    }

    #[test]
    fn test_bbox_coordinates_in_query() {
        let fetcher = OverpassFetcher::new();

        // SF tile at zoom 14
        let tile = TileCoord::from_latlon(37.7749, -122.4194, 14);
        let (min_lat, _min_lon, max_lat, _max_lon) = tile.bbox();

        let query = fetcher.build_query(&tile);

        // Query should contain all bbox coordinates
        assert!(query.contains(&min_lat.to_string()[..5])); // First 5 chars
        assert!(query.contains(&max_lat.to_string()[..5]));
        // lon is negative, so check format
        assert!(query.contains("-122."));
    }

    // Integration test (requires network, disabled by default)
    #[tokio::test]
    #[ignore]
    async fn test_fetch_tile_integration() {
        let mut fetcher = OverpassFetcher::new();

        // San Francisco tile (should have data)
        let tile = TileCoord::from_latlon(37.7749, -122.4194, 14);

        match fetcher.fetch_tile(tile).await {
            Ok(segments) => {
                assert!(!segments.is_empty());
                println!("Fetched {} segments", segments.len());

                // Verify segments have valid data
                for seg in segments.iter().take(5) {
                    assert!(seg.id > 0);
                    assert!(seg.geometry.0.len() >= 2);
                    println!("Segment {}: {} ({:?})", seg.id, seg.name.as_deref().unwrap_or("unnamed"), seg.road_class);
                }
            }
            Err(e) => {
                panic!("Fetch failed: {}", e);
            }
        }
    }

    #[tokio::test]
    #[ignore]
    async fn test_fetch_empty_tile() {
        let mut fetcher = OverpassFetcher::new();

        // Ocean tile (should have no roads)
        let tile = TileCoord::from_latlon(0.0, -160.0, 14);

        match fetcher.fetch_tile(tile).await {
            Ok(_) => {
                // Might succeed with empty data
            }
            Err(FetchError::NoData) => {
                // Expected for ocean tile
            }
            Err(e) => {
                println!("Fetch error (acceptable): {}", e);
            }
        }
    }
}
