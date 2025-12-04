/// Example: Trajectory Prediction Demo
///
/// Demonstrates the predict_trajectory() function with second-order integration
/// and "gravity well" Z-constraint for ground vehicles.
///
/// Outputs trajectory.csv for visualization in Python/Matplotlib.

use motion_tracker_rs::filters::ekf_15d::Ekf15d;
use std::fs::File;
use std::io::Write;

fn main() {
    println!("=== Trajectory Prediction Demo ===\n");

    // Initialize EKF with realistic parameters
    let dt = 0.05; // 50ms (20 Hz)
    let gps_noise = 5.0; // 5m std dev
    let accel_noise = 0.1; // 0.1 m/s² std dev
    let gyro_noise = 0.01; // 0.01 rad/s std dev

    let mut ekf = Ekf15d::new(dt, gps_noise, accel_noise, gyro_noise);

    // Set initial state: stationary at origin, level orientation
    ekf.set_state(
        (0.0, 0.0, 0.0),            // position
        (0.0, 0.0, 0.0),            // velocity
        (1.0, 0.0, 0.0, 0.0),       // quaternion (identity)
        (0.0, 0.0, 0.0),            // gyro bias
        (0.0, 0.0),                 // accel bias (X, Y only)
    );

    println!("Initial State:");
    println!("  Position: (0.0, 0.0, 0.0) m");
    println!("  Velocity: (0.0, 0.0, 0.0) m/s");
    println!("  Orientation: level (identity quaternion)\n");

    // Scenario 1: Forward acceleration (highway merge)
    println!("--- Scenario 1: Forward Acceleration ---");
    let accel_fwd = (2.0, 0.0, 9.81); // 2 m/s² forward, +9.81 Z (normal force when level)
    let gyro_fwd = (0.0, 0.0, 0.0);   // No rotation

    // Accelerate for 2 seconds
    for _ in 0..40 {
        ekf.predict(accel_fwd, gyro_fwd);
    }

    let state1 = ekf.get_state();
    println!("After 2s forward acceleration:");
    println!("  Velocity: ({:.2}, {:.2}, {:.2}) m/s",
        state1.velocity.0, state1.velocity.1, state1.velocity.2);
    println!("  Position: ({:.2}, {:.2}, {:.2}) m\n",
        state1.position.0, state1.position.1, state1.position.2);

    // Predict 3-second trajectory with current dynamics
    let horizon = 3.0; // 3 seconds
    let num_steps = 30; // 10 Hz prediction rate
    let trajectory = ekf.predict_trajectory(accel_fwd, gyro_fwd, horizon, num_steps, true);

    println!("Predicted trajectory (next 3 seconds, Z-constraint ON):");
    for (i, point) in trajectory.iter().enumerate() {
        if i % 10 == 9 {
            println!("  t={:.1}s: pos=({:.2}, {:.2}, {:.2}), vel=({:.2}, {:.2}, {:.2})",
                point.time,
                point.position.0, point.position.1, point.position.2,
                point.velocity.0, point.velocity.1, point.velocity.2);
        }
    }
    println!();

    // Scenario 2: Turning left (constant speed)
    println!("--- Scenario 2: Left Turn at Constant Speed ---");
    let accel_turn = (0.0, 0.0, 9.81); // No linear accel, just normal force
    let gyro_turn = (0.0, 0.0, 0.3);   // 0.3 rad/s yaw rate (17°/s)

    // Turn for 3 seconds
    for _ in 0..60 {
        ekf.predict(accel_turn, gyro_turn);
    }

    let state2 = ekf.get_state();
    println!("After 3s left turn:");
    println!("  Velocity: ({:.2}, {:.2}, {:.2}) m/s",
        state2.velocity.0, state2.velocity.1, state2.velocity.2);
    println!("  Position: ({:.2}, {:.2}, {:.2}) m",
        state2.position.0, state2.position.1, state2.position.2);
    println!("  Heading change: ~51° (0.3 rad/s × 3s × 57.3°/rad)\n");

    // Predict curved trajectory
    let trajectory_turn = ekf.predict_trajectory(accel_turn, gyro_turn, 2.0, 20, true);

    println!("Predicted turn trajectory (next 2 seconds):");
    for (i, point) in trajectory_turn.iter().enumerate() {
        if i % 5 == 4 {
            println!("  t={:.1}s: pos=({:.2}, {:.2}, {:.2})",
                point.time, point.position.0, point.position.1, point.position.2);
        }
    }
    println!();

    // Scenario 3: Z-axis drift test (unestimated Z-bias)
    println!("--- Scenario 3: Z-Axis Drift Test ---");
    println!("Simulating 0.1 m/s² Z-bias error (phone calibration drift)\n");

    // Reset to stationary
    ekf.set_state(
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0),
    );

    // Introduce Z-bias error (filter thinks Z-bias = 0, but phone has +0.1 m/s²)
    let accel_z_drift = (0.0, 0.0, 9.91); // Should be 9.81, but phone reads 9.91 (0.1 m/s² bias)
    let gyro_still = (0.0, 0.0, 0.0);

    // Compare predictions WITH and WITHOUT Z-constraint
    let traj_no_constraint = ekf.predict_trajectory(accel_z_drift, gyro_still, 5.0, 50, false);
    let traj_with_constraint = ekf.predict_trajectory(accel_z_drift, gyro_still, 5.0, 50, true);

    println!("5-second prediction comparison:");
    println!("  WITHOUT Z-constraint: final Z = {:.2} m (DRIFT!)",
        traj_no_constraint.last().unwrap().position.2);
    println!("  WITH Z-constraint:    final Z = {:.2} m (CORRECTED)",
        traj_with_constraint.last().unwrap().position.2);
    println!("\nZ-constraint prevents altitude drift from unestimated Z-bias!\n");

    // Performance test
    println!("--- Performance Test ---");
    let start = std::time::Instant::now();
    let num_predictions = 1000;
    for _ in 0..num_predictions {
        let _ = ekf.predict_trajectory(accel_fwd, gyro_fwd, 3.0, 30, true);
    }
    let elapsed = start.elapsed();
    println!("Generated {} predictions (30 steps each) in {:.2} ms",
        num_predictions, elapsed.as_secs_f64() * 1000.0);
    println!("Average: {:.2} μs per prediction\n",
        elapsed.as_secs_f64() * 1e6 / num_predictions as f64);

    // === CSV Export for Visualization ===
    println!("--- CSV Export ---");

    // Generate a complex trajectory for visualization
    ekf.set_state(
        (0.0, 0.0, 0.0),
        (15.0, 0.0, 0.0),  // 15 m/s forward (54 km/h)
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0),
    );

    // Highway turn: constant speed + gentle left turn
    let accel_highway = (0.0, 0.0, 9.81);
    let gyro_highway = (0.0, 0.0, 0.1); // 0.1 rad/s = 5.7°/s
    let trajectory_export = ekf.predict_trajectory(accel_highway, gyro_highway, 10.0, 100, true);

    // Export to CSV
    match File::create("trajectory.csv") {
        Ok(mut file) => {
            writeln!(file, "time,px,py,pz,vx,vy,vz").unwrap();
            for point in &trajectory_export {
                writeln!(
                    file,
                    "{:.3},{:.3},{:.3},{:.3},{:.3},{:.3},{:.3}",
                    point.time,
                    point.position.0, point.position.1, point.position.2,
                    point.velocity.0, point.velocity.1, point.velocity.2
                ).unwrap();
            }
            println!("Exported {} trajectory points to trajectory.csv", trajectory_export.len());
            println!("Visualize with: python3 plot_traj.py\n");
        }
        Err(e) => {
            eprintln!("Failed to create CSV: {}", e);
        }
    }

    println!("=== Demo Complete ===");
}
