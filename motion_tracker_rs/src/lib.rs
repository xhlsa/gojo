#![allow(unsafe_op_in_unsafe_fn)]
#![allow(non_local_definitions)]

use nalgebra::{DMatrix, DVector};
use ndarray::{arr1, Array1, Array2};
use numpy::{PyArray1, PyArray2, ToPyArray};
use pyo3::types::PyDict;
use pyo3::{exceptions::PyValueError, prelude::*};

pub mod factors;

/// Simple state update representing `x_new = x + v * dt`.
///
/// This mirrors the Python ES-EKF step so we can check parity between the two implementations.
#[pyfunction]
fn predict_position<'py>(
    py: Python<'py>,
    position: &PyArray1<f64>,
    velocity: &PyArray1<f64>,
    dt: f64,
) -> &'py PyArray1<f64> {
    let pos = position.readonly();
    let vel = velocity.readonly();
    let result = pos.as_array().to_owned() + vel.as_array().to_owned() * dt;
    result.to_pyarray(py)
}

/// Update covariance via `P_new = F * P * F^T + Q`.
#[pyfunction]
fn propagate_covariance<'py>(
    py: Python<'py>,
    f_matrix: &PyArray2<f64>,
    covariance: &PyArray2<f64>,
    process_noise: &PyArray2<f64>,
) -> PyResult<&'py PyArray2<f64>> {
    let f = f_matrix.readonly();
    let p = covariance.readonly();
    let q = process_noise.readonly();

    let tmp = f.as_array().dot(&p.as_array());
    let propagated = tmp.dot(&f.as_array().t()) + q.as_array();
    Ok(propagated.to_pyarray(py))
}

fn array2_from_nd(array: &Array2<f64>) -> PyResult<DMatrix<f64>> {
    let (rows, cols) = array.dim();
    let slice = array
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("Expected contiguous matrix data"))?;
    Ok(DMatrix::from_row_slice(rows, cols, slice))
}

fn array1_from_nd(array: &Array1<f64>) -> PyResult<DVector<f64>> {
    let slice = array
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("Expected contiguous vector data"))?;
    Ok(DVector::from_column_slice(slice))
}

fn kalman_update_arrays(
    state: &mut Array1<f64>,
    covariance: &mut Array2<f64>,
    measurement_matrix: &Array2<f64>,
    residual: &Array1<f64>,
    measurement_noise: &Array2<f64>,
) -> PyResult<()> {
    let state_len = state.len();
    let (cov_rows, cov_cols) = covariance.dim();
    if cov_rows != cov_cols || cov_rows != state_len {
        return Err(PyValueError::new_err(
            "Covariance must be square and match state dimension",
        ));
    }

    let (h_rows, h_cols) = measurement_matrix.dim();
    if h_cols != state_len {
        return Err(PyValueError::new_err(
            "Measurement matrix column count must match state dimension",
        ));
    }
    if residual.len() != h_rows {
        return Err(PyValueError::new_err(
            "Residual dimension must match measurement rows",
        ));
    }
    let (r_rows, r_cols) = measurement_noise.dim();
    if r_rows != h_rows || r_cols != h_rows {
        return Err(PyValueError::new_err(
            "Measurement noise must be square and match measurement dimension",
        ));
    }

    let mut x = array1_from_nd(state)?;
    let p = array2_from_nd(covariance)?;
    let h = array2_from_nd(measurement_matrix)?;
    let residual_vec = array1_from_nd(residual)?;
    let r = array2_from_nd(measurement_noise)?;

    let h_transpose = h.transpose();
    let s = &h * &p * &h_transpose + r.clone();
    let s_inv = s
        .try_inverse()
        .ok_or_else(|| PyValueError::new_err("Innovation covariance is singular"))?;
    let k = &p * &h_transpose * s_inv;
    let dx = &k * residual_vec;
    x += dx;

    let identity = DMatrix::identity(state_len, state_len);
    let i_kh = &identity - &k * &h;
    let new_p = &i_kh * p * i_kh.transpose() + &k * r * k.transpose();

    let state_vec: Vec<f64> = x.iter().copied().collect();
    for (slot, value) in state.iter_mut().zip(state_vec.iter()) {
        *slot = *value;
    }

    let cov_vec: Vec<f64> = new_p.iter().copied().collect();
    *covariance = Array2::from_shape_vec((state_len, state_len), cov_vec)
        .map_err(|e| PyValueError::new_err(format!("Failed to build covariance array: {e}")))?;
    Ok(())
}

#[pyfunction]
fn kalman_update<'py>(
    py: Python<'py>,
    state: &PyArray1<f64>,
    covariance: &PyArray2<f64>,
    measurement_matrix: &PyArray2<f64>,
    residual: &PyArray1<f64>,
    measurement_noise: &PyArray2<f64>,
) -> PyResult<(&'py PyArray1<f64>, &'py PyArray2<f64>)> {
    let state_ro = state.readonly();
    let cov_ro = covariance.readonly();
    let h_ro = measurement_matrix.readonly();
    let residual_ro = residual.readonly();
    let noise_ro = measurement_noise.readonly();

    let mut state_arr = state_ro.as_array().to_owned();
    let mut cov_arr = cov_ro.as_array().to_owned();
    let h_arr = h_ro.as_array().to_owned();
    let residual_arr = residual_ro.as_array().to_owned();
    let noise_arr = noise_ro.as_array().to_owned();

    kalman_update_arrays(
        &mut state_arr,
        &mut cov_arr,
        &h_arr,
        &residual_arr,
        &noise_arr,
    )?;

    Ok((state_arr.to_pyarray(py), cov_arr.to_pyarray(py)))
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

fn es_ekf_predict_arrays(
    state_vec: &mut Array1<f64>,
    covariance: &mut Array2<f64>,
    process_noise: &Array2<f64>,
    dt: f64,
) {
    let vx = state_vec[2];
    let vy = state_vec[3];
    let ax = state_vec[4];
    let ay = state_vec[5];
    let heading = state_vec[6];
    let heading_rate = state_vec[7];

    let vel_mag = (vx * vx + vy * vy).sqrt();
    let vx_pred = vel_mag * heading.cos();
    let vy_pred = vel_mag * heading.sin();

    let dt2 = dt * dt;
    state_vec[0] += vx_pred * dt + 0.5 * ax * dt2;
    state_vec[1] += vy_pred * dt + 0.5 * ay * dt2;
    state_vec[2] += ax * dt;
    state_vec[3] += ay * dt;
    state_vec[6] += heading_rate * dt;

    let f = build_es_ekf_jacobian(dt);
    let tmp = f.dot(&covariance.view());
    let propagated = tmp.dot(&f.t()) + process_noise;
    *covariance = propagated;
}

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

fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const R: f64 = 6_371_000.0;
    let d_lat = (lat2 - lat1).to_radians();
    let d_lon = (lon2 - lon1).to_radians();
    let a = (d_lat / 2.0).sin().powi(2)
        + lat1.to_radians().cos() * lat2.to_radians().cos() * (d_lon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().atan2((1.0 - a).max(0.0).sqrt());
    R * c
}

fn current_timestamp(py: Python<'_>) -> PyResult<f64> {
    let time_mod = py.import("time")?;
    let ts = time_mod.call_method0("time")?.extract::<f64>()?;
    Ok(ts)
}

#[pyclass]
struct PyEsEkf {
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

impl PyEsEkf {
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

    fn gps_measurement_jacobian() -> Array2<f64> {
        let mut h = Array2::<f64>::zeros((2, 8));
        h[[0, 0]] = 1.0;
        h[[1, 1]] = 1.0;
        h
    }

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

    fn velocity_magnitude(&self) -> f64 {
        (self.state[2] * self.state[2] + self.state[3] * self.state[3]).sqrt()
    }

    fn acceleration_magnitude(&self) -> f64 {
        (self.state[4] * self.state[4] + self.state[5] * self.state[5]).sqrt()
    }
}

#[pymethods]
impl PyEsEkf {
    #[new]
    #[pyo3(signature = (dt=0.02, gps_noise_std=8.0, accel_noise_std=0.5, enable_gyro=false, gyro_noise_std=0.1))]
    fn new(
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

    fn predict(&mut self, py: Python<'_>) -> PyResult<()> {
        es_ekf_predict_arrays(
            &mut self.state,
            &mut self.covariance,
            &self.process_noise,
            self.dt,
        );
        self.predict_count += 1;
        if self.last_position.is_some() {
            let vel_mag = self.velocity_magnitude();
            let now = current_timestamp(py)?;
            if self.last_gps_timestamp.map_or(true, |ts| now - ts > 1.0) {
                self.accumulated_distance += (vel_mag * self.dt).max(0.0);
            }
        }
        Ok(())
    }

    #[pyo3(signature = (latitude, longitude, gps_speed=None, gps_accuracy=None))]
    fn update_gps(
        &mut self,
        py: Python<'_>,
        latitude: f64,
        longitude: f64,
        gps_speed: Option<f64>,
        gps_accuracy: Option<f64>,
    ) -> PyResult<(f64, f64)> {
        let now = current_timestamp(py)?;
        if self.origin.is_none() {
            self.origin = Some((latitude, longitude));
            self.last_position = Some((latitude, longitude));
            self.last_gps_timestamp = Some(now);
            self.state[0] = 0.0;
            self.state[1] = 0.0;
            self.gps_update_count += 1;
            return Ok((0.0, 0.0));
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

        kalman_update_arrays(
            &mut self.state,
            &mut self.covariance,
            &measurement_matrix,
            &residual,
            &measurement_noise,
        )?;

        if let Some((lat_prev, lon_prev)) = self.last_position {
            let delta_dist = haversine_distance(lat_prev, lon_prev, latitude, longitude);
            self.accumulated_distance += delta_dist;
        }

        self.last_position = Some((latitude, longitude));
        self.last_gps_timestamp = Some(now);
        self.gps_update_count += 1;

        Ok((self.velocity_magnitude(), self.accumulated_distance))
    }

    fn update_accelerometer(&mut self, accel_magnitude: f64) -> PyResult<(f64, f64)> {
        let measurement_matrix = self.accel_measurement_jacobian();
        let ax = self.state[4];
        let ay = self.state[5];
        let z_pred = (ax * ax + ay * ay + 1e-9).sqrt();
        let residual = arr1(&[accel_magnitude - z_pred]);
        let measurement_noise = Self::measurement_noise_from_var(self.r_accel);

        kalman_update_arrays(
            &mut self.state,
            &mut self.covariance,
            &measurement_matrix,
            &residual,
            &measurement_noise,
        )?;

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
        Ok((vel_mag, self.accumulated_distance))
    }

    fn update_gyroscope(
        &mut self,
        _gyro_x: f64,
        _gyro_y: f64,
        gyro_z: f64,
    ) -> PyResult<(f64, f64)> {
        if !self.enable_gyro {
            let vel_mag = self.velocity_magnitude();
            return Ok((vel_mag, self.accumulated_distance));
        }

        let measurement_matrix = Self::gyro_measurement_jacobian();
        let residual = arr1(&[gyro_z - self.state[7]]);
        let measurement_noise = Self::measurement_noise_from_var(self.r_gyro);

        kalman_update_arrays(
            &mut self.state,
            &mut self.covariance,
            &measurement_matrix,
            &residual,
            &measurement_noise,
        )?;

        self.gyro_update_count += 1;
        Ok((self.velocity_magnitude(), self.accumulated_distance))
    }

    fn get_position(&self) -> PyResult<(f64, f64, f64)> {
        if let Some((origin_lat, origin_lon)) = self.origin {
            let (lat, lon) = meters_to_latlon(self.state[0], self.state[1], origin_lat, origin_lon);
            let uncertainty = ((self.covariance[[0, 0]] + self.covariance[[1, 1]]) / 2.0).sqrt();
            Ok((lat, lon, uncertainty))
        } else {
            Ok((0.0, 0.0, 999.9))
        }
    }

    fn get_state<'py>(&self, py: Python<'py>) -> PyResult<&'py PyDict> {
        let dict = PyDict::new(py);
        let (lat, lon, uncertainty) = self.get_position()?;
        let vel_mag = self.velocity_magnitude();
        let accel_mag = self.acceleration_magnitude();
        dict.set_item("position", (lat, lon))?;
        dict.set_item("position_local", (self.state[0], self.state[1]))?;
        dict.set_item("velocity", vel_mag)?;
        dict.set_item("velocity_vector", (self.state[2], self.state[3]))?;
        dict.set_item("acceleration", accel_mag)?;
        dict.set_item("acceleration_vector", (self.state[4], self.state[5]))?;
        dict.set_item("heading", self.state[6])?;
        dict.set_item("heading_deg", self.state[6].to_degrees())?;
        dict.set_item("heading_rate", self.state[7])?;
        dict.set_item("heading_rate_degs", self.state[7].to_degrees())?;
        dict.set_item("distance", self.accumulated_distance)?;
        dict.set_item("uncertainty_m", uncertainty)?;
        let trace: f64 = (0..8).map(|i| self.covariance[[i, i]]).sum();
        dict.set_item("covariance_trace", trace)?;
        dict.set_item("gps_updates", self.gps_update_count)?;
        dict.set_item("accel_updates", self.accel_update_count)?;
        dict.set_item("gyro_updates", self.gyro_update_count)?;
        Ok(dict)
    }

    fn export_state<'py>(&self, py: Python<'py>) -> (&'py PyArray1<f64>, &'py PyArray2<f64>) {
        (
            self.state.to_owned().to_pyarray(py),
            self.covariance.to_owned().to_pyarray(py),
        )
    }

    fn reset(&mut self) {
        self.state[2] = 0.0;
        self.state[3] = 0.0;
        self.state[4] = 0.0;
        self.state[5] = 0.0;
        self.accumulated_distance = 0.0;
    }
}

#[pyfunction]
fn es_ekf_predict<'py>(
    py: Python<'py>,
    state: &PyArray1<f64>,
    covariance: &PyArray2<f64>,
    process_noise: &PyArray2<f64>,
    dt: f64,
) -> PyResult<(&'py PyArray1<f64>, &'py PyArray2<f64>)> {
    let state_ro = state.readonly();
    let mut state_vec = state_ro.as_array().to_owned();
    if state_vec.len() != 8 {
        return Err(PyValueError::new_err(
            "Error-state vector must have length 8",
        ));
    }

    let cov_ro = covariance.readonly();
    let cov_shape = cov_ro.shape();
    if cov_shape != [8, 8] {
        return Err(PyValueError::new_err("Covariance must be an 8x8 matrix"));
    }

    let q_ro = process_noise.readonly();
    let q_shape = q_ro.shape();
    if q_shape != [8, 8] {
        return Err(PyValueError::new_err("Process noise must be an 8x8 matrix"));
    }

    let mut cov_arr = cov_ro.as_array().to_owned();
    let q_arr = q_ro.as_array().to_owned();

    es_ekf_predict_arrays(&mut state_vec, &mut cov_arr, &q_arr, dt);

    Ok((state_vec.to_pyarray(py), cov_arr.to_pyarray(py)))
}

#[pymodule]
fn motion_tracker_rs(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(predict_position, m)?)?;
    m.add_function(wrap_pyfunction!(propagate_covariance, m)?)?;
    m.add_function(wrap_pyfunction!(kalman_update, m)?)?;
    m.add_function(wrap_pyfunction!(es_ekf_predict, m)?)?;
    m.add_class::<PyEsEkf>()?;
    Ok(())
}
