"""
Pluggable sensor fusion filter implementations.

This module provides a factory function to instantiate different filter implementations
that all conform to the same interface, allowing swappable sensor fusion strategies.

Example usage:
    fusion = get_filter('complementary')
    fusion = get_filter('kalman')

    velocity, distance = fusion.update_gps(lat, lon, speed, accuracy)
    velocity, distance = fusion.update_accelerometer(accel_magnitude)
    state = fusion.get_state()
"""

def get_filter(filter_type='ekf', **kwargs):
    """
    Factory function to get filter implementation by name.

    Args:
        filter_type (str): Filter type - options:
            - 'complementary': Simple weighted fusion (fast, good for testing)
            - 'kalman': Linear Kalman filter (requires filterpy)
            - 'kalman-numpy': Pure numpy Kalman filter (GPS+accel only)
            - 'ekf': Extended Kalman Filter (RECOMMENDED - handles gyro non-linearity)
            - 'ukf': Unscented Kalman Filter (most accurate, 7x slower, overkill)
        **kwargs: Additional arguments passed to filter constructor

    Returns:
        Filter instance with update_gps(), update_accelerometer(), get_state() methods

    Raises:
        ValueError: If filter_type is not recognized

    Note: EKF is recommended for production as it's positioned for gyro integration.
    Gyro measurements (orientation updates) are inherently non-linear and require
    Jacobian-based linearization that EKF provides natively via quaternion kinematics.
    """
    if filter_type == 'complementary':
        from .complementary import ComplementaryFilter
        return ComplementaryFilter(**kwargs)
    elif filter_type == 'kalman':
        from .kalman import KalmanFilter
        return KalmanFilter(**kwargs)
    elif filter_type == 'kalman-numpy':
        from .kalman_numpy import KalmanFilterNumpy
        return KalmanFilterNumpy(**kwargs)
    elif filter_type == 'ekf':
        from .ekf import ExtendedKalmanFilter
        return ExtendedKalmanFilter(**kwargs)
    elif filter_type == 'ukf':
        from .ukf import UnscentedKalmanFilter
        return UnscentedKalmanFilter(**kwargs)
    elif filter_type == 'es_ekf':
        from .es_ekf import ErrorStateEKF
        return ErrorStateEKF(**kwargs)
    else:
        raise ValueError(f"Unknown filter type: {filter_type}. Use 'complementary', 'kalman', 'kalman-numpy', 'ekf', 'ukf', or 'es_ekf'")


__all__ = ['get_filter']
