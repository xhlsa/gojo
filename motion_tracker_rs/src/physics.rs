/// Virtual Dyno Physics Engine
/// Calculates real-time specific power (Watts/kg) from accelerometer and velocity data
/// This is vehicle-agnostic - works for any mass by normalizing to power-to-weight ratio

const GRAVITY: f64 = 9.81; // m/s²
const MIN_SPEED_MS: f64 = 2.0; // Only calculate above 2 m/s (lower threshold without drag losses)

#[derive(Clone, Copy, Debug)]
pub struct SpecificPowerOutput {
    pub specific_power_w_per_kg: f64, // Watts per kilogram (vehicle-independent metric)
    pub power_coefficient: f64,       // Unitless power metric for dashboard
}

impl Default for SpecificPowerOutput {
    fn default() -> Self {
        Self {
            specific_power_w_per_kg: 0.0,
            power_coefficient: 0.0,
        }
    }
}

/// Calculate specific power from corrected acceleration and speed
///
/// Physics: P_specific = |a_net| × v
/// Where:
///   - a_net: corrected acceleration (filtered_accel - gravity_bias) in m/s²
///   - v: current velocity in m/s
///
/// This metric is mass-independent because:
/// - Corrected acceleration naturally includes effects of engine power, air resistance, and grade
/// - Dividing by mass (in unit analysis) gives us power-to-weight ratio
/// - The product a × v directly represents energy expenditure per unit mass
///
/// Usage: Call with filtered acceleration (gravity already subtracted) and EKF velocity
pub fn calculate_specific_power(
    accel_x: f64,
    accel_y: f64,
    accel_z: f64,
    velocity_ms: f64,
) -> SpecificPowerOutput {
    // Only calculate when speed > 2 m/s to avoid noise
    if velocity_ms < MIN_SPEED_MS {
        return SpecificPowerOutput::default();
    }

    // Calculate net acceleration magnitude (3D)
    // This includes kinematic acceleration + gravity component from uneven terrain
    let accel_magnitude = (accel_x * accel_x + accel_y * accel_y + accel_z * accel_z).sqrt();

    // Specific power: [m/s²] × [m/s] = [m²/s³] = [W/kg]
    // This is power-to-weight ratio (dimensionally equivalent to acceleration × velocity)
    let specific_power = accel_magnitude * velocity_ms;

    // Power coefficient: normalized metric (0-100+) for dashboard visualization
    // Scaling: ~100 W/kg = aggressive acceleration, ~50 W/kg = moderate, ~10 W/kg = light
    let power_coefficient = specific_power / GRAVITY; // Normalize by g for intuitive scaling

    SpecificPowerOutput {
        specific_power_w_per_kg: specific_power.max(0.0),
        power_coefficient: power_coefficient.max(0.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zero_speed() {
        let output = calculate_specific_power(1.0, 0.0, 0.0, 0.0);
        assert_eq!(output.specific_power_w_per_kg, 0.0);
    }

    #[test]
    fn test_below_threshold() {
        let output = calculate_specific_power(2.0, 0.0, 0.0, 1.5);
        assert_eq!(output.specific_power_w_per_kg, 0.0);
    }

    #[test]
    fn test_light_acceleration() {
        // 10 m/s (36 km/h) with 1 m/s² acceleration
        let output = calculate_specific_power(1.0, 0.0, 0.0, 10.0);

        // Specific power = |a| × v = 1.0 × 10.0 = 10 W/kg
        assert!(output.specific_power_w_per_kg > 0.0);
        assert!((output.specific_power_w_per_kg - 10.0).abs() < 0.01);

        // Power coefficient = 10.0 / 9.81 ≈ 1.02
        assert!((output.power_coefficient - 10.0 / GRAVITY).abs() < 0.01);
    }

    #[test]
    fn test_aggressive_acceleration() {
        // 20 m/s (72 km/h) with 5 m/s² acceleration
        let output = calculate_specific_power(5.0, 0.0, 0.0, 20.0);

        // Specific power = |a| × v = 5.0 × 20.0 = 100 W/kg
        assert!((output.specific_power_w_per_kg - 100.0).abs() < 0.01);

        // Power coefficient = 100.0 / 9.81 ≈ 10.19
        assert!((output.power_coefficient - 100.0 / GRAVITY).abs() < 0.01);
    }

    #[test]
    fn test_3d_acceleration_magnitude() {
        // 3D acceleration: sqrt(3² + 4²) = 5 m/s² at 10 m/s
        // Specific power = 5 × 10 = 50 W/kg
        let output = calculate_specific_power(3.0, 4.0, 0.0, 10.0);

        assert!((output.specific_power_w_per_kg - 50.0).abs() < 0.01);
    }
}
