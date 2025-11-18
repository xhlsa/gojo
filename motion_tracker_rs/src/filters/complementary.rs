use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ComplementaryFilterState {
    pub position: (f64, f64),
    pub velocity: f64,
    pub heading: f64,
    pub heading_deg: f64,
    pub distance: f64,
}

pub struct ComplementaryFilter {
    // State vector: [x, y, vx, vy, heading]
    x: f64,
    y: f64,
    vx: f64,
    vy: f64,
    heading: f64,

    // GPS state for complementary filter
    last_gps_lat: Option<f64>,
    last_gps_lon: Option<f64>,
    last_gps_time: Option<f64>,

    accumulated_distance: f64,
    gps_updates: u64,

    // Filter gains
    gps_weight: f64,        // 0.7 = 70% trust GPS
    accel_weight: f64,      // 0.3 = 30% trust accel
}

impl ComplementaryFilter {
    pub fn new() -> Self {
        Self {
            x: 0.0,
            y: 0.0,
            vx: 0.0,
            vy: 0.0,
            heading: 0.0,
            last_gps_lat: None,
            last_gps_lon: None,
            last_gps_time: None,
            accumulated_distance: 0.0,
            gps_updates: 0,
            gps_weight: 0.7,
            accel_weight: 0.3,
        }
    }

    pub fn update(&mut self, ax: f64, ay: f64, _az: f64, _gx: f64, _gy: f64, _gz: f64) {
        let dt = 0.05; // 50ms timestep

        // Integrate acceleration to velocity (accel-based estimate)
        self.vx += ax * dt * self.accel_weight;
        self.vy += ay * dt * self.accel_weight;

        // Integrate velocity to position
        self.x += self.vx * dt;
        self.y += self.vy * dt;

        // Update heading from velocity
        let vel_mag = (self.vx * self.vx + self.vy * self.vy).sqrt();
        if vel_mag > 0.1 {
            self.heading = self.vy.atan2(self.vx);
        }
    }

    pub fn update_gps(&mut self, lat: f64, lon: f64) {
        let now = current_timestamp();

        if self.last_gps_lat.is_none() {
            // First GPS fix
            self.last_gps_lat = Some(lat);
            self.last_gps_lon = Some(lon);
            self.last_gps_time = Some(now);
            self.x = 0.0;
            self.y = 0.0;
            self.gps_updates += 1;
            return;
        }

        let prev_lat = self.last_gps_lat.unwrap();
        let prev_lon = self.last_gps_lon.unwrap();
        let prev_time = self.last_gps_time.unwrap();

        // Convert to local coordinates
        let (gps_x, gps_y) = latlon_to_meters(lat, lon, prev_lat, prev_lon);

        // GPS provides position estimate
        let dt = (now - prev_time).max(0.01);

        // Complementary filter: blend accel-integrated position with GPS
        // 70% GPS (authoritative for position), 30% accel (high-freq detail)
        self.x = self.gps_weight * gps_x + self.accel_weight * self.x;
        self.y = self.gps_weight * gps_y + self.accel_weight * self.y;

        // GPS velocity (from position difference)
        if dt > 0.01 {
            let gps_vx = gps_x / dt;
            let gps_vy = gps_y / dt;

            // Blend with accel-integrated velocity
            self.vx = self.gps_weight * gps_vx + self.accel_weight * self.vx;
            self.vy = self.gps_weight * gps_vy + self.accel_weight * self.vy;
        }

        // Update heading from GPS bearing
        let d_lon = (lon - prev_lon).to_radians();
        let lat_rad = lat.to_radians();
        let prev_lat_rad = prev_lat.to_radians();
        let numerator = d_lon.sin() * lat_rad.cos();
        let denominator = prev_lat_rad.cos() * lat_rad.sin()
            - prev_lat_rad.sin() * lat_rad.cos() * d_lon.cos();
        let gps_bearing = numerator.atan2(denominator);

        self.heading = self.gps_weight * gps_bearing + self.accel_weight * self.heading;

        // Accumulate distance
        let delta_dist = haversine_distance(prev_lat, prev_lon, lat, lon);
        self.accumulated_distance += delta_dist;

        self.last_gps_lat = Some(lat);
        self.last_gps_lon = Some(lon);
        self.last_gps_time = Some(now);
        self.gps_updates += 1;
    }

    pub fn velocity_magnitude(&self) -> f64 {
        (self.vx * self.vx + self.vy * self.vy).sqrt()
    }

    pub fn get_state(&self) -> Option<ComplementaryFilterState> {
        Some(ComplementaryFilterState {
            position: (self.x, self.y),
            velocity: self.velocity_magnitude(),
            heading: self.heading,
            heading_deg: self.heading.to_degrees(),
            distance: self.accumulated_distance,
        })
    }

    #[allow(dead_code)]
    pub fn get_velocity(&self) -> f64 {
        self.velocity_magnitude()
    }
}

fn latlon_to_meters(lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat - origin_lat).to_radians();
    let d_lon = (lon - origin_lon).to_radians();
    let x = R * d_lon * origin_lat.to_radians().cos();
    let y = R * d_lat;
    (x, y)
}

fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat2 - lat1).to_radians();
    let d_lon = (lon2 - lon1).to_radians();
    let a = (d_lat / 2.0).sin().powi(2)
        + lat1.to_radians().cos()
            * lat2.to_radians().cos()
            * (d_lon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().atan2((1.0 - a).max(0.0).sqrt());
    R * c
}

fn current_timestamp() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
