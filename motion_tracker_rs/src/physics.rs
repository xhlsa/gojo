/// Virtual Dyno Physics Engine
/// Calculates real-time horsepower from GPS speed and accelerometer data

#[derive(Clone, Copy)]
pub struct VehicleParams {
    pub mass_kg: f64,           // Vehicle + passenger mass (kg)
    pub cd: f64,                // Drag coefficient (dimensionless)
    pub frontal_area: f64,      // Frontal area (m²)
    pub rolling_resistance: f64, // Rolling resistance coefficient
    pub tire_radius: f64,       // Tire radius for torque calculation (m)
}

impl Default for VehicleParams {
    fn default() -> Self {
        Self {
            mass_kg: 1600.0,
            cd: 0.30,
            frontal_area: 2.2,
            rolling_resistance: 0.015,
            tire_radius: 0.35,
        }
    }
}

const AIR_DENSITY: f64 = 1.225; // kg/m³ at sea level
const GRAVITY: f64 = 9.81; // m/s²
const HP_TO_WATTS: f64 = 745.7;
const MIN_SPEED_MS: f64 = 5.0; // Only calculate above 5 m/s

#[derive(Clone, Copy, Debug)]
pub struct PowerOutput {
    pub horsepower: f64,
    pub torque_nm: f64,
    pub power_watts: f64,
    pub force_n: f64,
}

impl Default for PowerOutput {
    fn default() -> Self {
        Self {
            horsepower: 0.0,
            torque_nm: 0.0,
            power_watts: 0.0,
            force_n: 0.0,
        }
    }
}

pub fn calculate_horsepower(
    speed_ms: f64,
    accel_x: f64,
    params: VehicleParams,
) -> PowerOutput {
    // Only calculate when speed > 5 m/s to avoid noise
    if speed_ms < MIN_SPEED_MS {
        return PowerOutput::default();
    }

    // Aerodynamic drag force: F_aero = 0.5 × ρ × Cd × A × v²
    let f_aero = 0.5 * AIR_DENSITY * params.cd * params.frontal_area * speed_ms * speed_ms;

    // Rolling resistance force: F_roll = m × g × Crr
    // (assuming flat road; gravity component already in accel_x)
    let f_roll = params.mass_kg * GRAVITY * params.rolling_resistance;

    // Kinematic force from acceleration: F_kinetic = m × a_x
    // Note: accel_x already includes gravity component from sensor fusion
    let f_kinetic = params.mass_kg * accel_x;

    // Total traction force needed: F_total = F_kinetic + F_aero + F_roll
    let force_n = f_kinetic + f_aero + f_roll;

    // Power: P = F × v
    let power_watts = force_n * speed_ms;

    // Horsepower: HP = W / 745.7
    let horsepower = power_watts / HP_TO_WATTS;

    // Torque: τ = F × r (at tire contact point)
    let torque_nm = force_n * params.tire_radius;

    PowerOutput {
        horsepower: horsepower.max(0.0), // Clamp negative values (coasting/braking)
        torque_nm: torque_nm.max(0.0),
        power_watts: power_watts.max(0.0),
        force_n,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zero_speed() {
        let params = VehicleParams::default();
        let output = calculate_horsepower(0.0, 1.0, params);
        assert_eq!(output.horsepower, 0.0);
    }

    #[test]
    fn test_below_threshold() {
        let params = VehicleParams::default();
        let output = calculate_horsepower(3.0, 2.0, params);
        assert_eq!(output.horsepower, 0.0);
    }

    #[test]
    fn test_typical_acceleration() {
        let params = VehicleParams::default();
        // 30 m/s (108 km/h) with 2 m/s² acceleration
        let output = calculate_horsepower(30.0, 2.0, params);

        // Verify output is positive and reasonable
        assert!(output.horsepower > 0.0);
        assert!(output.torque_nm > 0.0);
        assert!(output.power_watts > 0.0);

        // Rough validation: 150 HP at 108 km/h is reasonable
        assert!(output.horsepower < 500.0); // Should be under 500 HP for this car
    }
}
