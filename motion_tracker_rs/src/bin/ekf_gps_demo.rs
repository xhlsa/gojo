/// Diagnostic: Compare scalar vs full GPS position update
/// 
/// Run this to see how cross-covariance affects trajectory tracking.
/// The key insight: when GPS corrects position, it should ALSO adjust
/// velocity estimates through the P_pv cross-covariance block.

use ndarray::{arr1, Array1, Array2};

/// Simplified demo showing the problem
fn main() {
    println!("=== GPS Update Cross-Covariance Demo ===\n");
    
    // Simulated state: moving North at 10 m/s, but position estimate drifted East
    let state = Array1::from_vec(vec![
        50.0, 0.0, 0.0,     // position: (50m East error, 0 North, 0 Up)
        0.0, 10.0, 0.0,     // velocity: (0 East, 10 North, 0 Up) - correct
        1.0, 0.0, 0.0, 0.0, // quaternion (identity)
        0.0, 0.0, 0.0,      // gyro bias
        0.0, 0.0,           // accel bias
    ]);
    
    // Covariance with position-velocity correlation
    // When moving, position uncertainty grows in the direction of velocity
    let mut p = Array2::<f64>::eye(15) * 10.0;
    
    // Add realistic cross-covariance: P_pv (position depends on velocity over time)
    // If we've been integrating velocity -> position, errors correlate
    p[[0, 3]] = 5.0;  // East position correlates with East velocity
    p[[3, 0]] = 5.0;
    p[[1, 4]] = 15.0; // North position strongly correlates with North velocity (main direction)
    p[[4, 1]] = 15.0;
    
    // GPS measurement: true position is (0, 0, 0) - we're at origin
    let gps_pos = (0.0, 0.0, 0.0);
    let gps_noise = 25.0; // 5m std -> 25 m² variance
    
    println!("Before GPS update:");
    println!("  Position: ({:.1}, {:.1}, {:.1}) m", state[0], state[1], state[2]);
    println!("  Velocity: ({:.1}, {:.1}, {:.1}) m/s", state[3], state[4], state[5]);
    println!("  P_pv cross-cov (East): {:.1}", p[[0, 3]]);
    println!("  P_pv cross-cov (North): {:.1}", p[[1, 4]]);
    
    // ========== SCALAR UPDATE (BROKEN) ==========
    let mut state_scalar = state.clone();
    let mut p_scalar = p.clone();
    
    for i in 0..3 {
        let _innovation = gps_pos.0 - state_scalar[0]; // simplified
        let s = p_scalar[[i, i]] + gps_noise;
        let gain = p_scalar[[i, i]] / s;
        state_scalar[i] += gain * (if i == 0 { -50.0 } else { 0.0 }); // innovation
        p_scalar[[i, i]] *= 1.0 - gain;
    }
    
    println!("\n--- SCALAR UPDATE (broken) ---");
    println!("  Position: ({:.1}, {:.1}, {:.1}) m", state_scalar[0], state_scalar[1], state_scalar[2]);
    println!("  Velocity: ({:.1}, {:.1}, {:.1}) m/s  <- UNCHANGED!", state_scalar[3], state_scalar[4], state_scalar[5]);
    println!("  P_pv cross-cov (East): {:.1}  <- UNCHANGED (will diverge)", p_scalar[[0, 3]]);
    
    // ========== FULL UPDATE (CORRECT) ==========
    let mut state_full = state.clone();
    let mut p_full = p.clone();
    
    // H matrix: observes position
    let mut h = Array2::<f64>::zeros((3, 15));
    h[[0, 0]] = 1.0;
    h[[1, 1]] = 1.0;
    h[[2, 2]] = 1.0;
    
    // R matrix
    let mut r = Array2::<f64>::zeros((3, 3));
    r[[0, 0]] = gps_noise;
    r[[1, 1]] = gps_noise;
    r[[2, 2]] = gps_noise;
    
    // Innovation
    let innovation = arr1(&[-50.0, 0.0, 0.0]); // GPS says we're 50m West of estimate
    
    // S = H*P*H^T + R
    let h_t = h.t();
    let s = h.dot(&p_full).dot(&h_t) + &r;
    
    // Invert S (3x3) - simplified for demo
    let mut s_inv = Array2::<f64>::zeros((3, 3));
    s_inv[[0, 0]] = 1.0 / s[[0, 0]];
    s_inv[[1, 1]] = 1.0 / s[[1, 1]];
    s_inv[[2, 2]] = 1.0 / s[[2, 2]];
    
    // K = P*H^T*S^-1 (15x3)
    let k = p_full.dot(&h_t).dot(&s_inv);
    
    // State update: ALL states
    let dx = k.dot(&innovation);
    for i in 0..15 {
        state_full[i] += dx[i];
    }
    
    // Joseph form covariance
    let i_mat = Array2::<f64>::eye(15);
    let kh = k.dot(&h);
    let i_kh = &i_mat - &kh;
    p_full = i_kh.dot(&p_full).dot(&i_kh.t()) + k.dot(&r).dot(&k.t());
    
    println!("\n--- FULL UPDATE (correct) ---");
    println!("  Position: ({:.1}, {:.1}, {:.1}) m", state_full[0], state_full[1], state_full[2]);
    println!("  Velocity: ({:.1}, {:.1}, {:.1}) m/s  <- ALSO CORRECTED!", state_full[3], state_full[4], state_full[5]);
    println!("  P_pv cross-cov (East): {:.1}  <- Properly reduced", p_full[[0, 3]]);
    
    println!("\n=== KEY INSIGHT ===");
    println!("The full update corrected velocity by {:.2} m/s through cross-covariance.", 
             state_full[3] - state[3]);
    println!("This is because position error correlates with velocity error.");
    println!("Without this, your EKF position and velocity drift apart!");
}
