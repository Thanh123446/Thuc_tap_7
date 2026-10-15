import os
import re
import sys
import time
import unicodedata
import math
from numbers import Integral
from typing import Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from shapely.strtree import STRtree
import pyproj

app = FastAPI(title="Mini GIS Backend", description="Backend kiểm thử thuật toán GIS độc lập")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json") if os.path.exists(os.path.join(BASE_DIR, "data.json")) else os.path.join(BASE_DIR, "data_2.json")

GDF: Optional[gpd.GeoDataFrame] = None
PARCEL_GDF: Optional[gpd.GeoDataFrame] = None
SPATIAL_TREE: Optional[STRtree] = None
SPATIAL_GEOMETRY_POSITIONS: dict[bytes, list[int]] = {}
transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32648", always_xy=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def clean_text(text: str) -> str:
    if text is None or pd.isna(text):
        return ""
    normalized = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(
        character for character in normalized
        if not unicodedata.combining(character)
    ).replace("đ", "d").strip()


def _index_geodataframe(gdf: gpd.GeoDataFrame) -> None:
    global GDF, PARCEL_GDF, SPATIAL_TREE, SPATIAL_GEOMETRY_POSITIONS

    if gdf.empty:
        if "geometry" not in gdf.columns:
            gdf = gpd.GeoDataFrame(
                {"geometry": gpd.GeoSeries([], crs="EPSG:4326")},
                crs="EPSG:4326",
            )
        gdf["dien_tich_m2"] = pd.Series(dtype="float64")
        gdf["_search_text"] = pd.Series(dtype="object")
        GDF = gdf
        PARCEL_GDF = gdf.copy()
        SPATIAL_TREE = None
        SPATIAL_GEOMETRY_POSITIONS = {}
        return

    if gdf.crs is None:
        gdf.set_crs(epsg=4326, inplace=True)
    if gdf.crs.to_string() != "EPSG:32648":
        gdf = gdf.to_crs(epsg=32648)

    geometry_areas = gdf.geometry.area.round(2)
    stored_area_column = next(
        (column for column in ("dien_tich", "area_sqm", "area") if column in gdf.columns),
        None,
    )
    if stored_area_column is None:
        gdf["dien_tich_m2"] = geometry_areas
    else:
        stored_areas = pd.to_numeric(gdf[stored_area_column], errors="coerce")
        gdf["dien_tich_m2"] = stored_areas.where(stored_areas.notna(), geometry_areas).round(2)
    cols = [column for column in gdf.columns if column != "geometry"]
    search_series = pd.Series("", index=gdf.index, dtype="object")
    for column in cols:
        search_series += gdf[column].apply(clean_text) + " "
    gdf["_search_text"] = search_series

    GDF = gdf
    PARCEL_GDF = GDF[GDF.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    geometries = list(PARCEL_GDF.geometry.values)
    SPATIAL_TREE = STRtree(geometries) if geometries else None
    SPATIAL_GEOMETRY_POSITIONS = {}
    for position, geometry in enumerate(geometries):
        SPATIAL_GEOMETRY_POSITIONS.setdefault(geometry.wkb, []).append(position)


def _parcel_ids(frame: gpd.GeoDataFrame) -> list[str]:
    ids = []
    for index, row in frame.iterrows():
        value = row.get("id", row.get("parcel_code", row.get("ten", index)))
        if pd.isna(value):
            continue
        ids.append(str(value))
    return ids


def _tree_query_positions(query_geometry) -> list[int]:
    if SPATIAL_TREE is None:
        return []

    positions = []
    for item in SPATIAL_TREE.query(query_geometry):
        if isinstance(item, Integral):
            positions.append(int(item))
        else:
            positions.extend(SPATIAL_GEOMETRY_POSITIONS.get(item.wkb, []))
    return list(dict.fromkeys(positions))


def _require_gis_data() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    if GDF is None or GDF.empty or PARCEL_GDF is None:
        raise HTTPException(status_code=500, detail="Dữ liệu bản đồ chưa được nạp.")
    return GDF, PARCEL_GDF


def _find_parcel_indices_by_identifier(
    parcels: gpd.GeoDataFrame,
    identifier: str,
    identifier_columns: list[str],
):
    exact_matches = parcels.index[
        parcels[identifier_columns].apply(
            lambda row: any(clean_text(value) == identifier for value in row),
            axis=1,
        )
    ]
    if len(exact_matches) or not (number_match := re.fullmatch(r"(?:thua|parcel)\s*#?\s*0*(\d+)", identifier)):
        return exact_matches

    parcel_number = str(int(number_match.group(1)))
    aliases = []
    if "id" in parcels.columns:
        aliases.append(
            parcels["id"].apply(
                lambda value: bool(
                    re.fullmatch(r"t0*" + re.escape(parcel_number), clean_text(value))
                )
            )
        )
    if "ten" in parcels.columns:
        name_pattern = re.compile(
            r"(?:^|\s)thua\s*0*" + re.escape(parcel_number) + r"\b"
        )
        aliases.append(
            parcels["ten"].apply(
                lambda value: bool(name_pattern.search(clean_text(value)))
            )
        )
    if not aliases:
        return parcels.index[:0]
    matches = aliases[0]
    for alias in aliases[1:]:
        matches |= alias
    return parcels.index[matches]


def load_geojson_features(features: list[dict]) -> None:
    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    _index_geodataframe(gdf)


@app.on_event("startup")
def load_data():
    if not os.path.exists(DATA_FILE):
        print(f"[LỖI CRITICAL] Không tìm thấy file dữ liệu tại: {BASE_DIR}")
        return

    try:
        t0 = time.time()
        gdf = gpd.read_file(DATA_FILE)
        _index_geodataframe(gdf)
        print(f"[OK SUCCESS] Đã nạp thành công {len(GDF)} đối tượng ({round((time.time() - t0)*1000, 2)} ms)")
    except Exception as e:
        print(f"[LỖI CRITICAL] Nạp data thất bại: {str(e)}")
# ------------------------------------------------------------------------------
# TRANG CHỦ: GIAO DIỆN KIỂM THỬ TRỰC TIẾP TẠI CHỖ (HTML + JS)
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home_ui():
    file_name = os.path.basename(DATA_FILE) if os.path.exists(DATA_FILE) else "Chưa có"
    count = len(GDF) if GDF is not None else 0

    html_content = f"""
    <!DOCTYPE html>
    <html lang="vi">
    <head>
        <meta charset="UTF-8">
        <title>Mini GIS Backend - Test trực tiếp</title>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 20px; background-color: #f4f6f9; color: #333; }}
            h2 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px; }}
            .status {{ background: #e8f8f5; border: 1px solid #2ecc71; padding: 10px 15px; border-radius: 6px; margin-bottom: 20px; font-size: 14px; }}
            .card {{ background: white; padding: 15px 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 15px; }}
            .card h3 {{ margin-top: 0; color: #2980b9; font-size: 16px; }}
            .form-group {{ margin-bottom: 10px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
            label {{ font-weight: bold; min-width: 120px; font-size: 13px; }}
            input, select {{ padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px; }}
            button {{ background-color: #3498db; color: white; border: none; padding: 7px 15px; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 13px; }}
            button:hover {{ background-color: #2980b9; }}
            pre {{ background: #272822; color: #f8f8f2; padding: 15px; border-radius: 6px; overflow-x: auto; max-height: 300px; font-size: 13px; }}
        </style>
    </head>
    <body>
        <h2>Mini GIS Backend - Trình kiểm thử thuật toán tại chỗ</h2>
        
        <div class="status">
            <b>Trạng thái:</b> Online | <b>File dữ liệu:</b> {file_name} | <b>Tổng đối tượng:</b> {count}
        </div>

        <!-- 1. LỌC THỬA -->
        <div class="card">
            <h3>1. Lọc thửa đất theo từ khóa (/api/loc-thua)</h3>
            <div class="form-group">
                <label>Từ khóa tìm kiếm:</label>
                <input type="text" id="kw" value="Lúa" style="width: 200px;">
                <button onclick="testLocThua()">Kiểm thử ngay</button>
            </div>
        </div>

        <!-- 2. THỬA CỰC TRỊ -->
        <div class="card">
            <h3>2. Tìm thửa lớn nhất / nhỏ nhất (/api/thua-cuc-tri)</h3>
            <div class="form-group">
                <label>Trường tính toán:</label>
                <input type="text" id="truong" value="dien_tich" style="width: 150px;">
                <label>Loại cực trị:</label>
                <select id="loai">
                    <option value="max">Lớn nhất (MAX)</option>
                    <option value="min">Nhỏ nhất (MIN)</option>
                </select>
                <button onclick="testThuaCucTri()">Kiểm thử ngay</button>
            </div>
        </div>

        <!-- 3. THỬA GẦN -->
        <div class="card">
            <h3>3. Tìm thửa gần điểm Lat/Lon (/api/thua-gan)</h3>
            <div class="form-group">
                <label>Vĩ độ (Lat):</label>
                <input type="number" step="0.0001" id="lat" value="19.973" style="width: 100px;">
                <label>Kinh độ (Lon):</label>
                <input type="number" step="0.0001" id="lon" value="105.936" style="width: 100px;">
                <label>Bán kính (m):</label>
                <input type="number" id="bk" value="500" style="width: 80px;">
                <button onclick="testThuaGan()">Kiểm thử ngay</button>
            </div>
        </div>

        <!-- 4. KHOẢNG CÁCH -->
        <div class="card">
            <h3>4. Tính khoảng cách 2 đối tượng (/api/khoang-cach)</h3>
            <div class="form-group">
                <label>Đối tượng 1:</label>
                <input type="text" id="dt1" value="giếng" style="width: 120px;">
                <label>Đối tượng 2:</label>
                <input type="text" id="dt2" value="thửa 1" style="width: 120px;">
                <button onclick="testKhoangCach()">Kiểm thử ngay</button>
            </div>
        </div>

        <!-- KHUNG HIỂN THỊ KẾT QUẢ -->
        <h3>Kết quả trả về (JSON):</h3>
        <pre id="output">Bấm các nút "Kiểm thử ngay" ở trên để xem kết quả tại đây...</pre>

        <script>
            async function callAPI(url) {{
                document.getElementById('output').innerText = "Đang tải dữ liệu...";
                try {{
                    let res = await fetch(url);
                    let data = await res.json();
                    document.getElementById('output').innerText = JSON.stringify(data, null, 2);
                }} catch (err) {{
                    document.getElementById('output').innerText = "Lỗi gọi API: " + err;
                }}
            }}

            function testLocThua() {{
                let kw = encodeURIComponent(document.getElementById('kw').value);
                callAPI(`/api/loc-thua?dieu_kien=${{kw}}`);
            }}

            function testThuaCucTri() {{
                let truong = document.getElementById('truong').value;
                let loai = document.getElementById('loai').value;
                callAPI(`/api/thua-cuc-tri?truong=${{truong}}&loai=${{loai}}`);
            }}

            function testThuaGan() {{
                let lat = document.getElementById('lat').value;
                let lon = document.getElementById('lon').value;
                let bk = document.getElementById('bk').value;
                callAPI(`/api/thua-gan?lat=${{lat}}&lon=${{lon}}&ban_kinh_m=${{bk}}`);
            }}

            function testKhoangCach() {{
                let dt1 = encodeURIComponent(document.getElementById('dt1').value);
                let dt2 = encodeURIComponent(document.getElementById('dt2').value);
                callAPI(`/api/khoang-cach?dt1=${{dt1}}&dt2=${{dt2}}`);
            }}
        </script>
    </body>
    </html>
    """
    return html_content


# ------------------------------------------------------------------------------
# ENDPOINTS API GIỮ NGUYÊN TÍNH NĂNG
# ------------------------------------------------------------------------------
@app.get("/api/loc-thua", summary="1. Lọc thửa đất theo nhiều điều kiện")
def loc_thua(
    dieu_kien: Optional[str] = None,
    cay_trong: Optional[str] = None,
    do_am_toi_thieu: Optional[float] = None,
    do_am_toi_da: Optional[float] = None,
    gan_doi_tuong: Optional[str] = None,
    ban_kinh_m: Optional[float] = None,
):
    _, parcels = _require_gis_data()
    t0 = time.perf_counter()
    res = parcels.copy()

    if dieu_kien and dieu_kien.strip():
        keyword = clean_text(dieu_kien)
        res = res[res["_search_text"].str.contains(keyword, regex=False, na=False)]

    if cay_trong and cay_trong.strip():
        crop_column = next(
            (column for column in ("cay_trong", "loai_nong_san", "loai_dat") if column in res.columns),
            None,
        )
        if crop_column is None:
            raise HTTPException(status_code=422, detail="Dữ liệu thửa đất không có thuộc tính cây trồng.")
        crop_values = res[crop_column].apply(clean_text)
        res = res[crop_values.str.contains(clean_text(cay_trong), regex=False, na=False)]

    if do_am_toi_thieu is not None or do_am_toi_da is not None:
        humidity_limits = [
            value for value in (do_am_toi_thieu, do_am_toi_da)
            if value is not None
        ]
        if any(not math.isfinite(value) or not 0 <= value <= 100 for value in humidity_limits):
            raise HTTPException(status_code=422, detail="Ngưỡng độ ẩm phải nằm trong khoảng 0 đến 100%.")
        if (
            do_am_toi_thieu is not None
            and do_am_toi_da is not None
            and do_am_toi_thieu > do_am_toi_da
        ):
            raise HTTPException(status_code=422, detail="Độ ẩm tối thiểu không được lớn hơn độ ẩm tối đa.")
        humidity_column = next(
            (column for column in ("do_am", "do_am_pct", "humidity") if column in res.columns),
            None,
        )
        if humidity_column is None:
            raise HTTPException(status_code=422, detail="Dữ liệu thửa đất không có thuộc tính độ ẩm.")
        humidity = pd.to_numeric(res[humidity_column], errors="coerce")
        valid_humidity = humidity.notna()
        if do_am_toi_thieu is not None:
            valid_humidity &= humidity >= do_am_toi_thieu
        if do_am_toi_da is not None:
            valid_humidity &= humidity <= do_am_toi_da
        res = res[valid_humidity]

    if (gan_doi_tuong is None) != (ban_kinh_m is None):
        raise HTTPException(
            status_code=422,
            detail="Lọc theo khoảng cách cần có cả đối tượng mốc và bán kính mét.",
        )
    if ban_kinh_m is not None:
        if not math.isfinite(ban_kinh_m) or ban_kinh_m < 0:
            raise HTTPException(status_code=422, detail="Bán kính phải là số hữu hạn không âm.")
        all_objects, _ = _require_gis_data()
        anchor_keyword = clean_text(gan_doi_tuong or "")
        anchors = all_objects[
            all_objects["_search_text"].str.contains(anchor_keyword, regex=False, na=False)
        ]
        if anchors.empty:
            raise HTTPException(
                status_code=404,
                detail=f"Không tìm thấy đối tượng mốc '{gan_doi_tuong}'.",
            )
        distances = res.geometry.apply(lambda geometry: anchors.geometry.distance(geometry).min())
        res = res[distances <= ban_kinh_m].copy()
        res["khoang_cach_m"] = distances.loc[res.index].round(2)

    cols = [column for column in res.columns if column not in ["geometry", "_search_text"]]
    records = res[cols].fillna("").head(20).to_dict(orient="records")
    return {
        "thoi_gian_ms": round((time.perf_counter() - t0) * 1000, 3),
        "tong_so": len(res),
        "parcel_ids": _parcel_ids(res),
        "du_lieu": records,
    }


@app.get("/api/thua-cuc-tri", summary="2. Tìm thửa lớn nhất / nhỏ nhất")
def thua_cuc_tri(
    truong: str = Query("dien_tich", description="Cột tính toán (dien_tich, do_am...)"),
    loai: str = Query("max", description="max hoặc min")
):
    _, parcels = _require_gis_data()
    normalized_type = clean_text(loai)
    if normalized_type not in {"max", "min", "lon_nhat", "nho_nhat"}:
        raise HTTPException(status_code=422, detail="Loại cực trị chỉ nhận 'max' hoặc 'min'.")

    field_aliases = {
        "dien_tich": "dien_tich_m2",
        "dien_tich_m2": "dien_tich_m2",
        "area_sqm": "dien_tich_m2",
        "area": "dien_tich_m2",
        "do_am": "do_am",
        "do_am_pct": "do_am_pct",
        "humidity": "humidity",
        "nhiet_do": "nhiet_do",
        "temperature": "temperature",
    }
    normalized_field = clean_text(truong)
    col = field_aliases.get(normalized_field, normalized_field)
    allowed_columns = {"dien_tich_m2", "do_am", "do_am_pct", "humidity", "nhiet_do", "temperature"}
    if col not in allowed_columns or col not in parcels.columns:
        raise HTTPException(
            status_code=422,
            detail=f"Trường cực trị '{truong}' không hợp lệ hoặc không có trong dữ liệu thửa đất.",
        )

    numeric_values = pd.to_numeric(parcels[col], errors="coerce")
    valid_values = numeric_values.dropna()
    if valid_values.empty:
        raise HTTPException(status_code=422, detail=f"Cột '{col}' không chứa dữ liệu số hợp lệ.")

    index = valid_values.idxmax() if normalized_type in {"max", "lon_nhat"} else valid_values.idxmin()
    result = parcels.loc[index]
    cols = [column for column in parcels.columns if column not in ["geometry", "_search_text"]]
    result_data = result[cols].fillna("").to_dict()
    return {
        "loai": "MAX" if normalized_type in {"max", "lon_nhat"} else "MIN",
        "truong": col,
        "parcel_ids": _parcel_ids(parcels.loc[[index]]),
        "du_lieu": result_data,
    }


def tinh_tong_dien_tich(
    dieu_kien: Optional[str] = None,
    danh_sach_thua: Optional[list[str]] = None,
):
    _, parcelas = _require_gis_data()
    if danh_sach_thua is not None:
        identifiers = [clean_text(item) for item in danh_sach_thua if clean_text(item)]
        if not identifiers:
            raise HTTPException(status_code=422, detail="Danh sách thửa đất không được để trống.")

        identifier_columns = [
            column for column in ("id", "ten", "parcel_code", "name")
            if column in parcelas.columns
        ]
        if not identifier_columns:
            raise HTTPException(
                status_code=422,
                detail="Dữ liệu thửa đất chưa có mã hoặc tên để chọn thửa cụ thể.",
            )

        selected_indices = []
        for identifier in identifiers:
            matches = _find_parcel_indices_by_identifier(
                parcelas,
                identifier,
                identifier_columns,
            )
            if len(matches) == 0:
                raise HTTPException(
                    status_code=404,
                    detail=f"Không tìm thấy thửa đất '{identifier}'.",
                )
            if len(matches) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"Mã hoặc tên thửa '{identifier}' không duy nhất; hãy dùng mã thửa.",
                )
            if matches[0] not in selected_indices:
                selected_indices.append(matches[0])

        parcelas = parcelas.loc[selected_indices]

    if dieu_kien and dieu_kien.strip():
        keyword = clean_text(dieu_kien)
        parcelas = parcelas[parcelas["_search_text"].str.contains(keyword, regex=False, na=False)]

    selected_parcels = [
        {
            "id": str(row.get("id", row.get("parcel_code", row.name))),
            "ten": str(row.get("ten", "")),
            "dien_tich_m2": round(float(row["dien_tich_m2"]), 2),
        }
        for _, row in parcelas.iterrows()
    ]
    return {
        "dieu_kien": dieu_kien or (
            "Danh sách thửa cụ thể" if danh_sach_thua is not None else "Tất cả thửa đất"
        ),
        "so_thua": int(len(parcelas)),
        "tong_dien_tich_m2": round(float(parcelas["dien_tich_m2"].sum()), 2),
        "parcel_ids": _parcel_ids(parcelas),
        "cac_thua_da_tinh": selected_parcels,
    }


@app.get("/api/tong-dien-tich", summary="Tính tổng diện tích các thửa đất")
def api_tinh_tong_dien_tich(
    dieu_kien: Optional[str] = Query(None, description="Từ khóa lọc thửa, ví dụ: Lúa"),
    danh_sach_thua: Optional[list[str]] = Query(
        None,
        description="Mã hoặc tên thửa cần tính; truyền lặp tham số, ví dụ T1 và T2",
    ),
):
    return tinh_tong_dien_tich(
        dieu_kien=dieu_kien,
        danh_sach_thua=danh_sach_thua,
    )


@app.get("/api/thua-gan", summary="3. Tìm thửa gần điểm Lat/Lon")
def thua_gan(
    lat: float = Query(..., description="Vĩ độ (VD: 19.973)"),
    lon: float = Query(..., description="Kinh độ (VD: 105.936)"),
    ban_kinh_m: float = Query(500.0, description="Bán kính mét (VD: 500)")
):
    _, parcels = _require_gis_data()
    if SPATIAL_TREE is None:
        raise HTTPException(status_code=404, detail="Không có polygon thửa đất trong dữ liệu bản đồ.")
    if not all(math.isfinite(value) for value in (lat, lon, ban_kinh_m)):
        raise HTTPException(status_code=422, detail="Tọa độ và bán kính phải là số hữu hạn.")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise HTTPException(status_code=422, detail="Tọa độ vĩ độ/kinh độ nằm ngoài phạm vi hợp lệ.")
    if ban_kinh_m < 0:
        raise HTTPException(status_code=422, detail="Bán kính phải là số không âm.")

    t0 = time.perf_counter()
    x, y = transformer.transform(lon, lat)
    pt = Point(x, y)
    positions = _tree_query_positions(pt.buffer(ban_kinh_m))
    if not positions:
        return {
            "thoi_gian_ms": round((time.perf_counter() - t0) * 1000, 3),
            "tong_so": 0,
            "parcel_ids": [],
            "du_lieu": [],
        }

    sub = parcels.iloc[positions].copy()
    sub["khoang_cach_m"] = sub.geometry.distance(pt).round(2)
    res = sub[sub["khoang_cach_m"] <= ban_kinh_m].sort_values("khoang_cach_m")

    cols = [c for c in res.columns if c not in ["geometry", "_search_text"]]
    records = res[cols].fillna("").head(10).to_dict(orient="records")
    return {
        "thoi_gian_ms": round((time.perf_counter() - t0) * 1000, 3),
        "tong_so": len(res),
        "parcel_ids": _parcel_ids(res.head(10)),
        "du_lieu": records
    }


@app.get("/api/khoang-cach", summary="4. Tính khoảng cách 2 đối tượng")
def khoang_cach(dt1: str, dt2: str):
    objects, parcels = _require_gis_data()

    k1, k2 = clean_text(dt1), clean_text(dt2)
    if not k1 or not k2:
        raise HTTPException(status_code=422, detail="Cần nhập từ khóa cho cả hai đối tượng.")
    def matching_objects(keyword):
        if keyword.startswith(("thua", "parcel")):
            return parcels
        return objects[objects["_search_text"].str.contains(keyword, regex=False, na=False)]

    g1 = matching_objects(k1)
    g2 = matching_objects(k2)

    if g1.empty:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy đối tượng 1 với từ khóa '{dt1}'")
    if g2.empty:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy đối tượng 2 với từ khóa '{dt2}'")

    nearest = None
    for index_1, object_1 in g1.iterrows():
        for index_2, object_2 in g2.iterrows():
            if index_1 == index_2:
                continue
            distance = float(object_1.geometry.distance(object_2.geometry))
            if nearest is None or distance < nearest[0]:
                nearest = (distance, index_1, object_1, index_2, object_2)
    if nearest is None:
        raise HTTPException(status_code=422, detail="Cần chọn hai đối tượng khác nhau để tính khoảng cách.")

    distance, index_1, object_1, index_2, object_2 = nearest

    def object_label(row, index):
        for column in ("ten", "name", "parcel_code", "id"):
            value = row.get(column)
            if value is not None and not pd.isna(value) and str(value).strip():
                return str(value)
        return str(index)

    parcel_indices = [
        index for index, row in ((index_1, object_1), (index_2, object_2))
        if row.geometry.geom_type in {"Polygon", "MultiPolygon"}
    ]
    parcel_ids = _parcel_ids(parcels.loc[parcel_indices]) if parcel_indices else []
    return {
        "doi_tuong_1": dt1,
        "doi_tuong_2": dt2,
        "doi_tuong_1_tim_duoc": object_label(object_1, index_1),
        "doi_tuong_2_tim_duoc": object_label(object_2, index_2),
        "id_1": str(object_1.get("id", index_1)),
        "id_2": str(object_2.get("id", index_2)),
        "khoang_cach_met": round(distance, 2),
        "parcel_ids": parcel_ids,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server_gis:app", host="127.0.0.1", port=8001, reload=True)
