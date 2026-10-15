import unittest

from fastapi import HTTPException

import server_gis


def polygon_feature(parcel_id, name, crop, moisture, area, west, south):
    east = west + 0.0001
    north = south + 0.0001
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]],
        },
        "properties": {
            "id": parcel_id,
            "ten": name,
            "cay_trong": crop,
            "do_am": moisture,
            "dien_tich": area,
        },
    }


def point_feature(object_id, name, lon, lat):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"id": object_id, "name": name, "dien_tich": 0},
    }


class GISAlgorithmTests(unittest.TestCase):
    def setUp(self):
        server_gis.load_geojson_features([
            polygon_feature("T1", "Thửa 1 - Đầm Thượng", "Lúa", 40, 100, 105.9000, 19.9000),
            polygon_feature("T2", "Thửa 2 - Bãi Rau Xanh", "Rau", 20, 200, 105.9010, 19.9000),
            polygon_feature("T3", "Thửa 3 - Vườn Cam", "Rau", 35, 300, 105.9030, 19.9000),
            point_feature("M-far", "Giếng xa", 105.9050, 19.90005),
            point_feature("M-near", "Giếng gần", 105.9012, 19.90005),
        ])

    def test_extreme_only_considers_parcels_and_rejects_invalid_type(self):
        result = server_gis.thua_cuc_tri("dien_tich", "min")
        self.assertEqual(result["parcel_ids"], ["T1"])
        with self.assertRaises(HTTPException) as error:
            server_gis.thua_cuc_tri("dien_tich", "middle")
        self.assertEqual(error.exception.status_code, 422)

    def test_nearby_search_returns_only_parcel_polygons(self):
        result = server_gis.thua_gan(19.90005, 105.9012, 75)
        self.assertEqual(result["parcel_ids"], ["T2"])
        self.assertTrue(all(item["id"].startswith("T") for item in result["du_lieu"]))

    def test_distance_uses_nearest_matching_pair(self):
        result = server_gis.khoang_cach("thửa", "giếng")
        self.assertEqual(result["doi_tuong_1_tim_duoc"], "Thửa 2 - Bãi Rau Xanh")
        self.assertEqual(result["doi_tuong_2_tim_duoc"], "Giếng gần")
        self.assertEqual(result["parcel_ids"], ["T2"])

    def test_multi_condition_filter_applies_every_condition(self):
        result = server_gis.loc_thua(
            cay_trong="rau",
            do_am_toi_da=30,
            gan_doi_tuong="gieng",
            ban_kinh_m=150,
        )
        self.assertEqual(result["parcel_ids"], ["T2"])

    def test_multi_condition_filter_requires_complete_distance_pair(self):
        with self.assertRaises(HTTPException) as error:
            server_gis.loc_thua(gan_doi_tuong="giếng")
        self.assertEqual(error.exception.status_code, 422)

    def test_total_area_uses_saved_values_for_selected_parcels(self):
        result = server_gis.tinh_tong_dien_tich(danh_sach_thua=["T1", "T2"])
        self.assertEqual(result["parcel_ids"], ["T1", "T2"])
        self.assertEqual(result["tong_dien_tich_m2"], 300)

    def test_total_area_resolves_common_parcel_number_names(self):
        result = server_gis.tinh_tong_dien_tich(danh_sach_thua=["thửa 1", "thửa 3"])
        self.assertEqual(result["parcel_ids"], ["T1", "T3"])
        self.assertEqual(result["tong_dien_tich_m2"], 400)

    def test_nearby_search_rejects_invalid_coordinates_and_radius(self):
        with self.assertRaises(HTTPException) as bad_coordinate:
            server_gis.thua_gan(95, 105.9, 100)
        self.assertEqual(bad_coordinate.exception.status_code, 422)
        with self.assertRaises(HTTPException) as bad_radius:
            server_gis.thua_gan(19.9, 105.9, -1)
        self.assertEqual(bad_radius.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
