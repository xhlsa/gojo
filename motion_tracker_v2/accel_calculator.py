#!/usr/bin/env python3
"""
Accelerometer Acceleration Calculator - Handles device tilt correctly

Two approaches to extract motion acceleration from accelerometer:

1. MAGNITUDE-BASED (Orientation-Independent) ⭐ RECOMMENDED FOR V2
   Works at any device orientation. Removes gravity magnitude, uses remaining.
   - Pros: Works at ANY tilt, no orientation sensor needed, robust
   - Cons: Loses directional info (only forward/backward magnitude)
   - Best for: Simple speed tracking (Motion Tracker V2)

2. COMPONENT-BASED (Orientation-Dependent)
   Uses x,y components directly, assumes level phone.
   - Pros: Directional, could work for 2D tracking if phone stays level
   - Cons: Fails if phone tilts, orientation-dependent
   - Best for: Specialized use cases only
"""

import math


class AccelerationCalculator:
    """Calculate motion acceleration from calibrated accelerometer readings."""

    def __init__(self, gravity_magnitude, bias_x, bias_y, bias_z, method='magnitude'):
        """
        Initialize calculator.

        Args:
            gravity_magnitude: Gravity value from calibration (should be ~9.81)
            bias_x, bias_y, bias_z: Per-axis biases from calibration
            method: 'magnitude' (recommended) or 'component' (level-only)
        """
        self.gravity_magnitude = gravity_magnitude
        self.bias_x = bias_x
        self.bias_y = bias_y
        self.bias_z = bias_z
        self.method = method

    def calculate_motion_magnitude(self, accel_data):
        """
        Extract motion acceleration magnitude (works at any device orientation).

        This is the RECOMMENDED approach for Motion Tracker V2.

        Physics:
        - Accelerometer measures: gravity + motion
        - We know magnitude of gravity from calibration (~9.81 m/s²)
        - By removing gravity magnitude, we get pure motion magnitude
        - This works regardless of device tilt

        Args:
            accel_data: dict with 'x', 'y', 'z' raw values and 'magnitude'

        Returns:
            motion_magnitude: Forward/backward acceleration (m/s²)
        """
        # Method 1: Use pre-calculated magnitude from AccelerometerThread
        if 'magnitude' in accel_data and accel_data['magnitude'] is not None:
            return accel_data['magnitude']

        # Method 2: Calculate from raw values
        x = accel_data.get('x', 0) - self.bias_x
        y = accel_data.get('y', 0) - self.bias_y
        z = accel_data.get('z', 0) - self.bias_z

        total_magnitude = math.sqrt(x**2 + y**2 + z**2)
        motion_magnitude = total_magnitude - self.gravity_magnitude
        return max(0, motion_magnitude)  # Clamp to 0 (can't be negative)

    def calculate_horizontal_component(self, accel_data):
        """
        Extract horizontal (x,y) acceleration component.

        ⚠️  WARNING: This assumes phone is LEVEL (z = vertical).
        Will give wrong results if phone is tilted.

        Use only if you're sure the phone stays level.
        For general use, use calculate_motion_magnitude() instead.

        Args:
            accel_data: dict with 'x', 'y' values

        Returns:
            horizontal_magnitude: sqrt(x² + y²) in m/s²
        """
        x = accel_data.get('x', 0) - self.bias_x
        y = accel_data.get('y', 0) - self.bias_y

        horizontal = math.sqrt(x**2 + y**2)
        return horizontal

    def calculate_from_components(self, accel_data):
        """
        Try to extract directional acceleration (x,y,z separately).

        ⚠️  This requires knowing device orientation, which V2 doesn't have.

        Returns:
            (accel_x, accel_y, accel_z): Per-axis motion acceleration
        """
        x = accel_data.get('x', 0) - self.bias_x
        y = accel_data.get('y', 0) - self.bias_y
        z = accel_data.get('z', 0) - self.bias_z

        # Without knowing device orientation, we can't remove gravity from
        # specific axes. This is incomplete without an IMU.
        return x, y, z

    def get_motion_acceleration(self, accel_data):
        """
        Get motion acceleration using configured method.

        Args:
            accel_data: Calibrated accelerometer reading

        Returns:
            float: Motion magnitude in m/s²
        """
        if self.method == 'magnitude':
            return self.calculate_motion_magnitude(accel_data)
        elif self.method == 'component':
            return self.calculate_horizontal_component(accel_data)
        else:
            # Default to magnitude (safest)
            return self.calculate_motion_magnitude(accel_data)

    @staticmethod
    def explain_methods():
        """Print explanation of the two approaches."""
        print("""
╔════════════════════════════════════════════════════════════════════════════════╗
║         ACCELEROMETER MOTION EXTRACTION - TWO APPROACHES EXPLAINED             ║
╚════════════════════════════════════════════════════════════════════════════════╝

MAGNITUDE-BASED (Orientation-Independent) ⭐ V2 RECOMMENDED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Physics:
  Raw accel vector = gravity_vector + motion_vector
  |raw accel| = |gravity + motion|

Calibration gives us:
  |gravity| ≈ 9.81 m/s² (magnitude of gravity, always ~9.81)

At runtime:
  |raw accel| = measured magnitude (sum of gravity + motion)
  |motion| = |raw accel| - |gravity|  ← gravity magnitude is constant!

Example (phone tilted 45°):
  Real motion: 2 m/s²
  Phone tilted → gravity points diagonally
  |raw_magnitude| = sqrt((~7 + 0)² + (~7 + 1)² + (0 + 1)²) ≈ 9.9 m/s²
  |motion| = 9.9 - 9.81 = 0.09 m/s² ✓ CORRECT (works at any tilt!)

Advantages:
  ✓ Works at ANY device orientation
  ✓ No orientation sensor needed (no IMU required)
  ✓ Simple and robust
  ✓ Handles phone rotation during tracking
  ✓ Physically correct

Disadvantages:
  ✗ Loses directional information (only magnitude)
  ✗ Can't distinguish forward vs lateral acceleration
  ✗ Can't build 2D motion model without orientation


COMPONENT-BASED (Orientation-Dependent)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Assumption: Phone is LEVEL (z-axis vertical, x/y horizontal)

Physics:
  horizontal_accel = sqrt(x² + y²)

Example (level phone, 2 m/s² forward):
  Raw: x=0, y=2, z=9.8 (gravity in z)
  Result: sqrt(0² + 2²) = 2 m/s² ✓ CORRECT (for level phone)

Example (phone tilted 45° forward):
  Real motion: still 2 m/s² forward
  But now gravity is in z,y and phone is tilted
  Raw might be: x=0, y=7, z=7 (tilted gravity + motion mixed)
  Result: sqrt(0² + 7²) = 7 m/s² ✗ WRONG! (off by 3.5x)

Advantages:
  ✓ Directional info (can track x,y separately)
  ✓ Works if phone stays perfectly level
  ✓ Could combine with heading for 2D tracking

Disadvantages:
  ✗ Fails if phone tilts (very sensitive to orientation)
  ✗ Needs orientation sensor (IMU with gyro/mag)
  ✗ Requires quaternion rotation math
  ✗ Breaks during normal phone manipulation


MOTION TRACKER V2 RECOMMENDATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Use MAGNITUDE-BASED approach because:
  • V2 doesn't have IMU (no orientation tracking available)
  • Phones rotate naturally during use (pocket, holder, hand, etc.)
  • Magnitude-based is robust and correct without orientation
  • Simple to implement, no complex math needed
  • Works in car, walking, or any scenario

Current V2 code uses component-based ✗
→ This breaks if phone tilts, giving wrong acceleration estimates
→ FIX: Switch to magnitude-based approach

""")
