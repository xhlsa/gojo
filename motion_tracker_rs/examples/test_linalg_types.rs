//! Minimal test to verify linalg types compile
use motion_tracker_rs::types::linalg::*;

fn main() {
    println!("State dimensions:");
    println!("  STATE_DIM_15 = {}", STATE_DIM_15);
    println!("  STATE_DIM_13 = {}", STATE_DIM_13);
    
    println!("\nMeasurement dimensions:");
    println!("  MEASURE_DIM_GPS_POS = {}", MEASURE_DIM_GPS_POS);
    println!("  MEASURE_DIM_GPS_VEL = {}", MEASURE_DIM_GPS_VEL);
    println!("  MEASURE_DIM_MAG = {}", MEASURE_DIM_MAG);
    println!("  MEASURE_DIM_BARO = {}", MEASURE_DIM_BARO);
    
    println!("\nUKF constants:");
    println!("  SIGMA_COUNT_15 = {}", SIGMA_COUNT_15);
    
    println!("\n✓ All linalg types accessible!");
}
