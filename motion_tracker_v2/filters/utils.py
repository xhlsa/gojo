"""
Shared mathematical utilities for sensor fusion filters.

Contains common functions used across multiple filters to avoid duplication
and ensure consistent calculations (e.g., haversine distance, lat/lon conversions).
"""

import math


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate distance between two GPS coordinates in meters.

    Args:
        lat1, lon1: First coordinate (latitude, longitude in degrees)
        lat2, lon2: Second coordinate (latitude, longitude in degrees)

    Returns:
        float: Distance in meters
    """
    R = 6371000  # Earth radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi/2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c


def latlon_to_meters(lat, lon, origin_lat, origin_lon):
    """
    Convert lat/lon to local x/y meters from origin using equirectangular projection.

    Args:
        lat, lon: Target coordinate (latitude, longitude in degrees)
        origin_lat, origin_lon: Origin coordinate (latitude, longitude in degrees)

    Returns:
        tuple: (x, y) in meters (local Cartesian coordinates)
    """
    R = 6371000  # Earth radius in meters

    lat_rad = math.radians(lat)
    origin_lat_rad = math.radians(origin_lat)

    x = R * math.radians(lon - origin_lon) * math.cos(origin_lat_rad)
    y = R * math.radians(lat - origin_lat)

    return x, y


def meters_to_latlon(x, y, origin_lat, origin_lon):
    """
    Convert local x/y meters to lat/lon relative to origin.

    Inverse of latlon_to_meters.

    Args:
        x, y: Local Cartesian coordinates in meters
        origin_lat, origin_lon: Origin coordinate (latitude, longitude in degrees)

    Returns:
        tuple: (latitude, longitude) in degrees
    """
    R = 6371000  # Earth radius in meters

    origin_lat_rad = math.radians(origin_lat)

    lat = origin_lat + math.degrees(y / R)
    lon = origin_lon + math.degrees(x / (R * math.cos(origin_lat_rad)))

    return lat, lon
