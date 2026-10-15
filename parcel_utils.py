from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform as transform_geometry


area_transformer = Transformer.from_crs("EPSG:4326", "EPSG:32648", always_xy=True)


def validate_parcel_geometry(geometry: dict) -> dict:
    try:
        polygon = shape(geometry)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("Ranh giới không phải GeoJSON hợp lệ.") from error

    if polygon.geom_type != "Polygon" or polygon.is_empty or not polygon.is_valid:
        raise ValueError("Ranh giới phải là Polygon hợp lệ, không tự giao cắt.")

    min_lon, min_lat, max_lon, max_lat = polygon.bounds
    if not (-180 <= min_lon <= max_lon <= 180 and -90 <= min_lat <= max_lat <= 90):
        raise ValueError("Tọa độ ranh giới nằm ngoài phạm vi kinh độ/vĩ độ hợp lệ.")
    return geometry


def parcel_area_sqm(geometry: dict) -> float:
    polygon = shape(geometry)
    projected = transform_geometry(area_transformer.transform, polygon)
    return round(projected.area, 2)
