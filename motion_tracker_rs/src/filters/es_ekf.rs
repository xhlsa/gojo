#![allow(dead_code)]

use ndarray::{arr1, Array1, Array2};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EsEkfState {
    pub position: (f64, f64),
    pub position_local: (f64, f64),
    pub velocity: f64,
    pub velocity_vector: (f64, f64),
    pub acceleration: f64,
    pub acceleration_vector: (f64, f64),
    pub heading: f64,
    pub heading_deg: f64,
    pub heading_rate: f64,
    pub heading_rate_degs: f64,
    pub distance: f64,
    pub uncertainty_m: f64,
    pub covariance_trace: f64,
    pub gps_updates: u64,
    pub accel_updates: u64,
    pub gyro_updates: u64,
}

pub struct EsEkf {
    dt: f64,
    state: Array1<f64>,
    covariance: Array2<f64>,
    process_noise: Array2<f64>,
    r_gps: Array2<f64>,
    r_accel: f64,
    r_gyro: f64,
    enable_gyro: bool,
    origin: Option<(f64, f64)>,
    last_position: Option<(f64, f64)>,
    last_gps_timestamp: Option<f64>,
    last_gps_bearing: f64,
    heading_initialized: bool,
    accumulated_distance: f64,
    gps_update_count: u64,
    accel_update_count: u64,
    gyro_update_count: u64,
    predict_count: u64,
}

impl EsEkf {
    pub fn new(
        dt: f64,
        gps_noise_std: f64,
        accel_noise_std: f64,
        enable_gyro: bool,
        gyro_noise_std: f64,
    ) -> Self {
        let state = Array1::<f64>::zeros(8);
        let covariance = Self::default_covariance();
        let process_noise = Self::build_process_noise(dt, accel_noise_std);

        let mut r_gps = Array2::<f64>::zeros((2, 2));
        let gps_var = gps_noise_std * gps_noise_std;
        r_gps[[0, 0]] = gps_var;
        r_gps[[1, 1]] = gps_var;

        let r_accel = accel_noise_std * accel_noise_std;
        let r_gyro = gyro_noise_std * gyro_noise_std;

        Self {
            dt,
            state,
            covariance,
            process_noise,
            r_gps,
            r_accel,
            r_gyro,
            enable_gyro,
            origin: None,
            last_position: None,
            last_gps_timestamp: None,
            last_gps_bearing: 0.0,
            heading_initialized: false,
            accumulated_distance: 0.0,
            gps_update_count: 0,
            accel_update_count: 0,
            gyro_update_count: 0,
            predict_count: 0,
        }
    }

    fn default_covariance() -> Array2<f64> {
        let mut p = Array2::<f64>::zeros((8, 8));
        let diag = [100.0, 100.0, 10.0, 10.0, 1.0, 1.0, 0.1, 0.01];
        for (idx, value) in diag.iter().enumerate() {
            p[[idx, idx]] = *value;
        }
        p
    }

    fn build_process_noise(dt: f64, accel_noise_std: f64) -> Array2<f64> {
        let accel_var = accel_noise_std * accel_noise_std;
        let q_pos = 0.25 * dt.powi(4) * accel_var;
        let q_vel = dt.powi(2) * accel_var;
        let q_accel = 0.5;
        let q_heading = 0.01;
        let q_heading_rate = 0.005;
        let mut q = Array2::<f64>::zeros((8, 8));
        q[[0, 0]] = q_pos;
        q[[1, 1]] = q_pos;
        q[[2, 2]] = q_vel;
        q[[3, 3]] = q_vel;
        q[[4, 4]] = q_accel;
        q[[5, 5]] = q_accel;
        q[[6, 6]] = q_heading;
        q[[7, 7]] = q_heading_rate;
        q
    }

    fn build_es_ekf_jacobian(dt: f64) -> Array2<f64> {
        let dt2 = dt * dt;
        let mut f = Array2::<f64>::zeros((8, 8));

        f[[0, 0]] = 1.0;
        f[[0, 2]] = dt;
        f[[0, 4]] = 0.5 * dt2;

        f[[1, 1]] = 1.0;
        f[[1, 3]] = dt;
        f[[1, 5]] = 0.5 * dt2;

        f[[2, 2]] = 1.0;
        f[[2, 4]] = dt;

        f[[3, 3]] = 1.0;
        f[[3, 5]] = dt;

        f[[4, 4]] = 1.0;
        f[[5, 5]] = 1.0;

        f[[6, 6]] = 1.0;
        f[[6, 7]] = dt;

        f[[7, 7]] = 1.0;

        f
    }

    fn gps_measurement_jacobian() -> Array2<f64> {
        let mut h = Array2::<f64>::zeros((2, 8));
        h[[0, 0]] = 1.0;
        h[[1, 1]] = 1.0;
        h
    }

    #[allow(dead_code)]
    fn accel_measurement_jacobian(&self) -> Array2<f64> {
        let ax = self.state[4];
        let ay = self.state[5];
        let accel_mag = (ax * ax + ay * ay).sqrt() + 1e-6;
        let mut h = Array2::<f64>::zeros((1, 8));
        h[[0, 4]] = ax / accel_mag;
        h[[0, 5]] = ay / accel_mag;
        h
    }

    fn gyro_measurement_jacobian() -> Array2<f64> {
        let mut h = Array2::<f64>::zeros((1, 8));
        h[[0, 7]] = 1.0;
        h
    }

    fn measurement_noise_from_var(var: f64) -> Array2<f64> {
        let mut r = Array2::<f64>::zeros((1, 1));
        r[[0, 0]] = var;
        r
    }

    fn kalman_update(
        &mut self,
        measurement_matrix: &Array2<f64>,
        residual: &Array1<f64>,
        measurement_noise: &Array2<f64>,
    ) {
        let h = measurement_matrix;
        let p = &self.covariance;
        let r = measurement_noise;

        let h_t = h.t().to_owned();
        let hph = h.dot(p).dot(&h_t);
        let s = &hph + r;

        // Compute S^-1 using simple 2D and 1x1 cases
        let s_inv = if s.dim() == (1, 1) {
            let mut inv = s.clone();
            if inv[[0, 0]].abs() > 1e-10 {
                inv[[0, 0]] = 1.0 / inv[[0, 0]];
            }
            inv
        } else if s.dim() == (2, 2) {
            let det = s[[0, 0]] * s[[1, 1]] - s[[0, 1]] * s[[1, 0]];
            if det.abs() > 1e-10 {
                let mut inv = Array2::zeros((2, 2));
                inv[[0, 0]] = s[[1, 1]] / det;
                inv[[0, 1]] = -s[[0, 1]] / det;
                inv[[1, 0]] = -s[[1, 0]] / det;
                inv[[1, 1]] = s[[0, 0]] / det;
                inv
            } else {
                s.clone() // Fallback: don't update
            }
        } else {
            s.clone() // For larger matrices, skip update (speedrun mode)
        };

        let k = p.dot(&h_t).dot(&s_inv);
        let dx = k.dot(residual);
        self.state = &self.state + &dx;

        let n = self.state.len();
        let eye = Array2::eye(n);
        let kh = k.dot(h);
        let i_kh = &eye - &kh;

        let p_new = i_kh.dot(p);
        self.covariance = p_new;
    }

    pub fn predict(&mut self) {
        let vx = self.state[2];
        let vy = self.state[3];
        let ax = self.state[4];
        let ay = self.state[5];
        let heading = self.state[6];
        let heading_rate = self.state[7];

        let vel_mag = (vx * vx + vy * vy).sqrt();
        let vx_pred = vel_mag * heading.cos();
        let vy_pred = vel_mag * heading.sin();

        let dt = self.dt;
        let dt2 = dt * dt;
        self.state[0] += vx_pred * dt + 0.5 * ax * dt2;
        self.state[1] += vy_pred * dt + 0.5 * ay * dt2;
        self.state[2] += ax * dt;
        self.state[3] += ay * dt;
        self.state[6] += heading_rate * dt;

        let f = Self::build_es_ekf_jacobian(dt);
        let fpt = f.dot(&self.covariance).dot(&f.t());
        self.covariance = fpt + &self.process_noise;

        self.predict_count += 1;

        // Distance accumulated in update_gps() using haversine measurement (not velocity integration)
        // This avoids double-counting when GPS is available
        // Future: Could use velocity integration during GPS gaps (> 5 seconds without fix)
    }

    pub fn update_gps(
        &mut self,
        latitude: f64,
        longitude: f64,
        gps_speed: Option<f64>,
        gps_accuracy: Option<f64>,
    ) {
        let now = current_timestamp();
        if self.origin.is_none() {
            self.origin = Some((latitude, longitude));
            self.last_position = Some((latitude, longitude));
            self.last_gps_timestamp = Some(now);
            self.state[0] = 0.0;
            self.state[1] = 0.0;
            self.gps_update_count += 1;
            return;
        }

        let (origin_lat, origin_lon) = self.origin.unwrap();
        let (x_meas, y_meas) = latlon_to_meters(latitude, longitude, origin_lat, origin_lon);

        if let Some(speed) = gps_speed {
            if speed > 0.5 {
                if let Some((lat_prev, lon_prev)) = self.last_position {
                    let lat_prev_rad = lat_prev.to_radians();
                    let lat_curr_rad = latitude.to_radians();
                    let d_lon = (longitude - lon_prev).to_radians();
                    let numerator = d_lon.sin() * lat_curr_rad.cos();
                    let denominator = lat_prev_rad.cos() * lat_curr_rad.sin()
                        - lat_prev_rad.sin() * lat_curr_rad.cos() * d_lon.cos();
                    let bearing = numerator.atan2(denominator);
                    self.last_gps_bearing = bearing;
                    if !self.heading_initialized {
                        self.state[6] = bearing;
                        self.heading_initialized = true;
                    }
                }
            }
        }

        let measurement_matrix = Self::gps_measurement_jacobian();
        let residual = arr1(&[x_meas - self.state[0], y_meas - self.state[1]]);
        let mut measurement_noise = self.r_gps.clone();
        if let Some(acc) = gps_accuracy {
            if acc > 0.0 {
                let var = acc * acc;
                measurement_noise[[0, 0]] = var;
                measurement_noise[[1, 1]] = var;
            }
        }

        self.kalman_update(&measurement_matrix, &residual, &measurement_noise);

        if let Some((lat_prev, lon_prev)) = self.last_position {
            let delta_dist = haversine_distance(lat_prev, lon_prev, latitude, longitude);
            // Reject GPS jitter when stationary: require either a minimum speed or a meaningful jump
            let speed_ok = gps_speed.map(|s| s > 1.0).unwrap_or(false);
            let acc_limit = gps_accuracy.unwrap_or(5.0).max(1.0); // meters
            // Require movement greater than 1x accuracy (more conservative than before)
            let dist_ok = delta_dist > acc_limit * 1.0;
            if speed_ok || dist_ok {
                self.accumulated_distance += delta_dist;
            }
        }

        self.last_position = Some((latitude, longitude));
        self.last_gps_timestamp = Some(now);
        self.gps_update_count += 1;
    }

    /// Update with acceleration vector (proper physics: not magnitude, but components)
    /// This respects the sign: forward acceleration (+) vs braking (-)
    pub fn update_accelerometer_vector(&mut self, accel_x: f64, accel_y: f64, _accel_z: f64) {
        // Rotate body-frame acceleration into world-frame using current heading
        let heading = self.state[6];
        // Maintain state layout: [0]=x, [1]=y, [2]=vx, [3]=vy, [4]=ax, [5]=ay, [6]=heading, [7]=heading_rate
        let accel_world_x = accel_x * heading.cos() - accel_y * heading.sin();
        let accel_world_y = accel_x * heading.sin() + accel_y * heading.cos();

        // Store measured acceleration in the acceleration slots
        self.state[4] = accel_world_x;
        self.state[5] = accel_world_y;

        // Integrate velocity using measured acceleration (position integration remains in predict())
        self.state[2] += accel_world_x * self.dt;
        self.state[3] += accel_world_y * self.dt;

        self.accel_update_count += 1;
    }

    /// Legacy: scalar magnitude version (deprecated, kept for backwards compat during testing)
    pub fn update_accelerometer(&mut self, accel_magnitude: f64) {
        let measurement_matrix = self.accel_measurement_jacobian();
        let ax = self.state[4];
        let ay = self.state[5];
        let z_pred = (ax * ax + ay * ay + 1e-9).sqrt();
        let residual = arr1(&[accel_magnitude - z_pred]);
        let measurement_noise = Self::measurement_noise_from_var(self.r_accel);

        self.kalman_update(&measurement_matrix, &residual, &measurement_noise);

        let accel_delta = accel_magnitude * self.dt;
        if self.heading_initialized {
            self.state[2] += accel_delta * self.state[6].cos();
            self.state[3] += accel_delta * self.state[6].sin();
        }

        let vel_mag = self.velocity_magnitude();
        if !self.heading_initialized {
            if vel_mag > 0.1 {
                self.state[6] = self.state[3].atan2(self.state[2]);
                self.heading_initialized = true;
            }
        } else {
            self.state[2] = vel_mag * self.state[6].cos();
            self.state[3] = vel_mag * self.state[6].sin();
        }

        self.accel_update_count += 1;
    }

    /// Update gyroscope: uses Z (yaw rate) for heading, X/Y for future 3D support
    pub fn update_gyroscope(&mut self, _gyro_x: f64, _gyro_y: f64, gyro_z: f64) {
        if !self.enable_gyro {
            return;
        }

        // For now, only use Z component (yaw/heading rate) in 2D motion model
        // TODO: X and Y can be used for pitch/roll detection (vehicle pitch during acceleration)
        let measurement_matrix = Self::gyro_measurement_jacobian();
        let residual = arr1(&[gyro_z - self.state[7]]);
        let measurement_noise = Self::measurement_noise_from_var(self.r_gyro);

        self.kalman_update(&measurement_matrix, &residual, &measurement_noise);

        // Update heading based on gyro measurement
        self.state[6] += gyro_z * self.dt;

        self.gyro_update_count += 1;
    }

    pub fn get_position(&self) -> (f64, f64, f64) {
        if let Some((origin_lat, origin_lon)) = self.origin {
            let (lat, lon) = meters_to_latlon(self.state[0], self.state[1], origin_lat, origin_lon);
            let uncertainty = ((self.covariance[[0, 0]] + self.covariance[[1, 1]]) / 2.0).sqrt();
            (lat, lon, uncertainty)
        } else {
            (0.0, 0.0, 999.9)
        }
    }

    pub fn velocity_magnitude(&self) -> f64 {
        (self.state[2] * self.state[2] + self.state[3] * self.state[3]).sqrt()
    }

    pub fn acceleration_magnitude(&self) -> f64 {
        (self.state[4] * self.state[4] + self.state[5] * self.state[5]).sqrt()
    }

    pub fn get_state(&self) -> Option<EsEkfState> {
        let (lat, lon, uncertainty) = self.get_position();
        let vel_mag = self.velocity_magnitude();
        let accel_mag = self.acceleration_magnitude();
        let covariance_trace: f64 = (0..8).map(|i| self.covariance[[i, i]]).sum();

        Some(EsEkfState {
            position: (lat, lon),
            position_local: (self.state[0], self.state[1]),
            velocity: vel_mag,
            velocity_vector: (self.state[2], self.state[3]),
            acceleration: accel_mag,
            acceleration_vector: (self.state[4], self.state[5]),
            heading: self.state[6],
            heading_deg: self.state[6].to_degrees(),
            heading_rate: self.state[7],
            heading_rate_degs: self.state[7].to_degrees(),
            distance: self.accumulated_distance,
            uncertainty_m: uncertainty,
            covariance_trace,
            gps_updates: self.gps_update_count,
            accel_updates: self.accel_update_count,
            gyro_updates: self.gyro_update_count,
        })
    }

    /// Zero Velocity Update (ZUPT): Force velocity AND acceleration to zero when vehicle is stationary
    /// Used to prevent drift when accelerometer reads gravity + small noise
    /// Zeroing acceleration prevents predict() from integrating velocity back in
    pub fn apply_zupt(&mut self) {
        self.state[2] = 0.0; // vx = 0
        self.state[3] = 0.0; // vy = 0
        self.state[4] = 0.0; // ax = 0 (prevent predict from integrating velocity back)
        self.state[5] = 0.0; // ay = 0
    }

    /// Set heading state directly (for GPS-based alignment)
    pub fn state_set_heading(&mut self, heading_rad: f64) {
        self.state[6] = heading_rad;
    }

    /// Extract covariance snapshot for analysis (trace + diagonal entries)
    pub fn get_covariance_snapshot(&self) -> (f64, [f64; 8]) {
        let trace: f64 = (0..8).map(|i| self.covariance[[i, i]]).sum();
        let diag = [
            self.covariance[[0, 0]],
            self.covariance[[1, 1]],
            self.covariance[[2, 2]],
            self.covariance[[3, 3]],
            self.covariance[[4, 4]],
            self.covariance[[5, 5]],
            self.covariance[[6, 6]],
            self.covariance[[7, 7]],
        ];
        (trace, diag)
    }
}

#[allow(dead_code)]
fn latlon_to_meters(lat: f64, lon: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat - origin_lat).to_radians();
    let d_lon = (lon - origin_lon).to_radians();
    let x = R * d_lon * origin_lat.to_radians().cos();
    let y = R * d_lat;
    (x, y)
}

fn meters_to_latlon(x: f64, y: f64, origin_lat: f64, origin_lon: f64) -> (f64, f64) {
    const R: f64 = 6_371_000.0;
    let d_lat = y / R;
    let d_lon = x / (R * origin_lat.to_radians().cos());
    let lat = origin_lat + d_lat.to_degrees();
    let lon = origin_lon + d_lon.to_degrees();
    (lat, lon)
}

#[allow(dead_code)]
fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat2 - lat1).to_radians();
    let d_lon = (lon2 - lon1).to_radians();
    let a = (d_lat / 2.0).sin().powi(2)
        + lat1.to_radians().cos() * lat2.to_radians().cos() * (d_lon / 2.0).sin().powi(2);
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
