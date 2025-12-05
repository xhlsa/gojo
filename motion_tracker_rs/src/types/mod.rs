pub mod linalg;

pub use linalg::*;

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct AccelData {
    pub timestamp: f64,
    pub x: f64,
    pub y: f64,
    pub z: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GyroData {
    pub timestamp: f64,
    pub x: f64,
    pub y: f64,
    pub z: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MagData {
    pub timestamp: f64,
    pub x: f64,
    pub y: f64,
    pub z: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GpsData {
    pub timestamp: f64,
    pub latitude: f64,
    pub longitude: f64,
    pub speed: f64,
    pub bearing: f64,
    pub accuracy: f64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BaroData {
    pub timestamp: f64,
    pub pressure_hpa: f64,
}
