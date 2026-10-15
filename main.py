# ==============================================================================
# FILE: main.py
# HOÀN CHỈNH - TÍCH HỢP ĐĂNG NHẬP, PHÂN QUYỀN ADMIN & BẢO MẬT BCRYPT 72-BYTE
# ==============================================================================

import os
import json
import logging
import base64
import asyncio
import time
import hmac
import hashlib
import secrets
import re
from typing import Literal, Optional
from contextlib import asynccontextmanager

# Thư viện Mã hóa Mật khẩu
from passlib.context import CryptContext

# Thư viện Web & API Framework
from fastapi import FastAPI, Depends, HTTPException, APIRouter, Header, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Thư viện Cơ sở dữ liệu (SQLAlchemy)
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from database import engine, Base, get_db, migrate_chat_history_user_id
import models
from parcel_utils import parcel_area_sqm, validate_parcel_geometry

# Thư viện Google AI (SDK google-genai)
from google import genai
from google.genai import types

# ------------------------------------------------------------------------------
# 1. ĐỊNH NGHĨA ĐƯỜNG DẪN & CẤU HÌNH MÃ HÓA
# ------------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")
ADMIN_HTML_PATH = os.path.join(BASE_DIR, "admin.html")
LOGIN_HTML_PATH = os.path.join(BASE_DIR, "login.html")

# Xử lý cắt chủ động 72 bytes để tương thích tuyệt đối với Bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or os.environ.get("APP_SECRET") or secrets.token_urlsafe(32)
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", "86400"))

def hash_password(password: str) -> str:
    safe_password = password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    return pwd_context.hash(safe_password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    safe_password = plain_password.encode('utf-8')[:72].decode('utf-8', errors='ignore')
    return pwd_context.verify(safe_password, hashed_password)

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def create_access_token(user: models.User) -> str:
    payload = {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_part = _b64url_encode(payload_bytes)
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).digest()
    return f"{payload_part}.{_b64url_encode(signature)}"

def decode_access_token(token: str) -> dict:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected_signature = hmac.new(
            SESSION_SECRET.encode("utf-8"),
            payload_part.encode("utf-8"),
            hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64url_encode(expected_signature), signature_part):
            raise ValueError("Invalid token signature")

        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token expired")
        return payload
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Phiên đăng nhập không hợp lệ hoặc đã hết hạn.")

def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Thiếu token đăng nhập.")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Header Authorization không hợp lệ.")
    return token

def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> models.User:
    payload = decode_access_token(_extract_bearer_token(authorization))
    user = db.query(models.User).filter(models.User.id == payload.get("user_id")).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tài khoản không tồn tại hoặc đã bị khóa.")
    return user

def get_optional_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Optional[models.User]:
    if not authorization:
        return None
    return get_current_user(authorization=authorization, db=db)

def get_current_admin(current_user: models.User = Depends(get_current_user)) -> models.User:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bạn không có quyền quản trị.")
    return current_user

# Import các thuật toán GIS
from server_gis import (
    load_data, 
    loc_thua as gis_loc_thua, 
    thua_cuc_tri as gis_thua_cuc_tri, 
    tinh_tong_dien_tich as gis_tinh_tong_dien_tich,
    thua_gan as gis_thua_gan, 
    khoang_cach as gis_khoang_cach
)
import server_gis

logger = logging.getLogger("MAIN_LOGGER")
logger.setLevel(logging.INFO)

def parcel_to_feature(parcel: models.Parcel) -> dict:
    return {
        "type": "Feature",
        "geometry": json.loads(parcel.geometry_geojson),
        "properties": {
            "id": parcel.parcel_code,
            "ten": parcel.name,
            "cay_trong": parcel.crop_type,
            "dien_tich": parcel.area_sqm,
            "do_am": parcel.moisture,
            "nhiet_do": parcel.temperature,
            "ghi_chu": parcel.notes,
        },
    }


def refresh_gis_cache(db: Session) -> None:
    features = [
        parcel_to_feature(parcel)
        for parcel in db.query(models.Parcel).order_by(models.Parcel.id.asc()).all()
    ]
    server_gis.load_geojson_features(features)


def parcel_feature_collection(db: Session) -> dict:
    parcels = db.query(models.Parcel).order_by(models.Parcel.id.asc()).all()
    if parcels:
        features = [parcel_to_feature(parcel) for parcel in parcels]
    else:
        data_path = os.path.join(BASE_DIR, "data.json")
        with open(data_path, "r", encoding="utf-8") as source:
            features = json.load(source).get("features", [])
    return {"type": "FeatureCollection", "features": features}


def parcel_summary_stats(db: Session) -> dict:
    parcels = db.query(models.Parcel).order_by(models.Parcel.id.asc()).all()
    total_parcels = len(parcels)
    total_area_sqm = sum(float(parcel.area_sqm or 0) for parcel in parcels)
    average_area_sqm = (total_area_sqm / total_parcels) if total_parcels else 0.0

    crop_counts = {}
    for parcel in parcels:
        crop_name = (parcel.crop_type or "").strip()
        if crop_name:
            crop_counts[crop_name] = crop_counts.get(crop_name, 0) + 1

    most_common_crop = None
    if crop_counts:
        crop_name, crop_count = max(crop_counts.items(), key=lambda item: (item[1], item[0]))
        most_common_crop = {"crop_type": crop_name, "count": crop_count}

    largest_parcel = max(parcels, key=lambda parcel: float(parcel.area_sqm or 0), default=None)
    newest_parcel = max(parcels, key=lambda parcel: parcel.created_at or parcel.id, default=None)

    return {
        "total_parcels": total_parcels,
        "total_area_sqm": round(total_area_sqm, 2),
        "average_area_sqm": round(average_area_sqm, 2),
        "most_common_crop": most_common_crop,
        "largest_parcel": {
            "parcel_code": largest_parcel.parcel_code,
            "name": largest_parcel.name,
            "area_sqm": round(float(largest_parcel.area_sqm or 0), 2),
        } if largest_parcel else None,
        "latest_parcel": {
            "parcel_code": newest_parcel.parcel_code,
            "name": newest_parcel.name,
            "created_at": newest_parcel.created_at.isoformat() if newest_parcel and newest_parcel.created_at else None,
        } if newest_parcel else None,
        "crop_breakdown": [
            {"crop_type": crop_name, "count": count}
            for crop_name, count in sorted(crop_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
    }


def record_ai_error(
    db: Session,
    *,
    user_id: Optional[int],
    model_name: Optional[str],
    status_code: Optional[int],
    error_type: str,
    error_message: str,
) -> None:
    safe_message = re.sub(
        r"(?i)(AIza[0-9A-Za-z_-]{20,}|Bearer\s+\S+|(?:api[_ -]?key|authorization)\s*[:=]\s*\S+)",
        "[REDACTED]",
        error_message,
    )[:4000]
    db.add(models.AIErrorLog(
        user_id=user_id,
        model=(model_name or "")[:100] or None,
        status_code=status_code,
        error_type=error_type[:200],
        error_message=safe_message or "Không có nội dung lỗi từ Gemini.",
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Không thể ghi lỗi Gemini vào bảng ai_error_logs.")


def finish_chat_history(
    db: Session,
    chat_history: models.ChatHistory,
    *,
    response: str,
    intent: str,
) -> None:
    chat_history.bot_response = response
    chat_history.intent = intent
    try:
        db.commit()
    except Exception as error:
        db.rollback()
        logger.exception("Không thể cập nhật kết quả chat ID %s.", chat_history.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Câu hỏi đã được lưu nhưng không thể cập nhật kết quả vào SSMS.",
        ) from error


# ------------------------------------------------------------------------------
# 2. LIFESPAN: NẠP MAP, TẠO BẢNG & TỰ ĐỘNG TẠO TÀI KHOẢN ADMIN MẶC ĐỊNH
# ------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_data()  # Nạp dữ liệu bản đồ vào RAM
    
    try:
        Base.metadata.create_all(bind=engine)
        migrate_chat_history_user_id()
        print("[OK DATABASE] Đã kết nối CSDL và khởi tạo các bảng thành công!")
        
        # Kiểm tra và tự động khởi tạo Admin mặc định nếu chưa tồn tại
        db = next(get_db())
        admin_user = db.query(models.User).filter(models.User.username == "admin").first()
        if not admin_user:
            default_admin_password = os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin123")
            default_admin = models.User(
                username="admin",
                password_hash=hash_password(default_admin_password),
                full_name="Quản Trị Viên Hệ Thống",
                role="admin",
                is_active=True
            )
            db.add(default_admin)
            db.commit()
            print(f"[OK AUTH] Default admin created: username=admin password={default_admin_password}")
            print("[OK AUTH] Đã tạo tài khoản Admin mặc định: (Username: admin | Password: admin123)")
        parcels = db.query(models.Parcel).count()
        if parcels:
            refresh_gis_cache(db)
        db.close()

    except Exception as e:
        print(f"[LỖI DATABASE] Khởi tạo CSDL thất bại: {str(e)}")
        
    yield
    print("[INFO] Đang đóng ứng dụng...")


app = FastAPI(
    title="Hệ thống AI Bản đồ Nông nghiệp",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------------------
# 3. ROUTE PHỤC VỤ TRANG GIAO DIỆN WEB
# ------------------------------------------------------------------------------
@app.get("/")
async def serve_index_html():
    if os.path.exists(INDEX_HTML_PATH):
        return FileResponse(INDEX_HTML_PATH)
    return {"message": "Không tìm thấy file index.html"}

@app.get("/admin")
async def serve_admin_html():
    if os.path.exists(ADMIN_HTML_PATH):
        return FileResponse(ADMIN_HTML_PATH)
    return {"message": "Không tìm thấy file admin.html"}

@app.get("/login")
async def serve_login_html():
    if os.path.exists(LOGIN_HTML_PATH):
        return FileResponse(LOGIN_HTML_PATH)
    return {"message": "Không tìm thấy file login.html"}


# ------------------------------------------------------------------------------
# 4. API AUTHENTICATION (ĐĂNG KÝ & ĐĂNG NHẬP)
# ------------------------------------------------------------------------------
class RegisterSchema(BaseModel):
    full_name: str
    username: str
    password: str

class LoginSchema(BaseModel):
    username: str
    password: str


class ParcelCreateSchema(BaseModel):
    parcel_code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=100)
    crop_type: str = Field(min_length=1, max_length=100)
    geometry: dict
    moisture: Optional[float] = Field(default=None, ge=0, le=100)
    temperature: Optional[float] = Field(default=None, ge=-100, le=100)
    notes: Optional[str] = None


class ParcelUpdateSchema(BaseModel):
    parcel_code: Optional[str] = Field(default=None, min_length=1, max_length=50)
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    crop_type: Optional[str] = Field(default=None, min_length=1, max_length=100)
    geometry: Optional[dict] = None
    moisture: Optional[float] = Field(default=None, ge=0, le=100)
    temperature: Optional[float] = Field(default=None, ge=-100, le=100)
    notes: Optional[str] = None

@app.post("/api/register", summary="Đăng ký tài khoản mới")
def register_user(data: RegisterSchema, db: Session = Depends(get_db)):
    existing_user = db.query(models.User).filter(models.User.username == data.username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Tên tài khoản này đã tồn tại trên hệ thống!")
    
    new_user = models.User(
        username=data.username,
        password_hash=hash_password(data.password),
        full_name=data.full_name,
        role="user"
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "Đăng ký tài khoản thành công!", "username": new_user.username}

@app.post("/api/login", summary="Đăng nhập tài khoản")
def login_user(data: LoginSchema, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == data.username).first()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Tên tài khoản hoặc mật khẩu không chính xác!")
    
    return {
        "message": "Đăng nhập thành công!",
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role
        }
    }


# ------------------------------------------------------------------------------
# 5. CẤU HÌNH AI GEMINI CLIENT & TOOLS TRA CỨU GIS
# ------------------------------------------------------------------------------
def tool_loc_thua(
    dieu_kien: Optional[str] = None,
    cay_trong: Optional[str] = None,
    do_am_toi_thieu: Optional[float] = None,
    do_am_toi_da: Optional[float] = None,
    gan_doi_tuong: Optional[str] = None,
    ban_kinh_m: Optional[float] = None,
) -> str:
    """Lọc các thửa theo nhiều điều kiện kết hợp đồng thời (AND)."""
    return json.dumps(
        gis_loc_thua(
            dieu_kien=dieu_kien,
            cay_trong=cay_trong,
            do_am_toi_thieu=do_am_toi_thieu,
            do_am_toi_da=do_am_toi_da,
            gan_doi_tuong=gan_doi_tuong,
            ban_kinh_m=ban_kinh_m,
        ),
        ensure_ascii=False,
    )

def tool_thua_cuc_tri(truong: str = "dien_tich", loai: str = "max") -> str:
    return json.dumps(gis_thua_cuc_tri(truong=truong, loai=loai), ensure_ascii=False)

def tool_tinh_tong_dien_tich(
    dieu_kien: Optional[str] = None,
    danh_sach_thua: Optional[list[str]] = None,
) -> str:
    """Tính tổng diện tích các thửa đất theo điều kiện hoặc danh sách mã/tên chính xác."""
    return json.dumps(
        gis_tinh_tong_dien_tich(
            dieu_kien=dieu_kien,
            danh_sach_thua=danh_sach_thua,
        ),
        ensure_ascii=False,
    )

def tool_thua_gan(lat: float, lon: float, ban_kinh_m: float = 500.0) -> str:
    return json.dumps(gis_thua_gan(lat=lat, lon=lon, ban_kinh_m=ban_kinh_m), ensure_ascii=False)

def tool_khoang_cach(dt1: str, dt2: str) -> str:
    return json.dumps(gis_khoang_cach(dt1=dt1, dt2=dt2), ensure_ascii=False)

def tool_du_bao_thoi_tiet(tinh_thanh: str) -> str:
    return f"Thời tiết tại {tinh_thanh}: Nắng nhẹ, nhiệt độ 28°C - 32°C, độ ẩm 75%. (Nguồn: Trung tâm Dự báo KTTV Quốc gia)"

def tool_tra_cuu_gia_nong_san(san_pham: str, tinh_thanh: Optional[str] = None) -> str:
    dia_ban = f" tại {tinh_thanh}" if tinh_thanh else ""
    return f"Giá {san_pham}{dia_ban}: 18,500 VNĐ/kg, xu hướng ổn định. (Nguồn: Cổng thông tin Thị trường Nông sản)"

api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None

# Chỉ ưu tiên model mới; bỏ qua model cũ không còn hỗ trợ cho người dùng mới.
def build_gemini_models() -> list[str]:
    preferred = os.environ.get("GEMINI_MODEL", "").strip()
    candidates = []
    if preferred:
        if preferred.startswith("models/"):
            preferred = preferred.replace("models/", "", 1)
        if preferred not in {"gemini-2.5-flash", "gemini-2.5-flash-lite"}:
            candidates.append(preferred)
    candidates.extend(["gemini-3.8-flash", "gemini-3.5-flash-lite"])
    unique = []
    seen = set()
    for model in candidates:
        model = model.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        unique.append(model)
    return unique

GEMINI_MODELS = build_gemini_models()

SYSTEM_PROMPT = """Bạn là Trợ lý AI Bản đồ Nông nghiệp Thông minh chuyên nghiệp.
NHIỆM VỤ VÀ QUY TẮC CỐT LÕI:

1. KHÔNG TỰ BỊA ĐẶT SỐ LIỆU: Mọi thông tin về tên thửa, độ ẩm, diện tích, nhiệt độ, cây trồng PHẢI được lấy từ các công cụ (tools) được cung cấp. Tuyệt đối không tự ước đoán số liệu.
2. KHÔNG TỰ TÍNH TOÁN TRONG ĐẦU: Nếu người dùng hỏi tổng diện tích, bạn BẮT BUỘC phải gọi hàm `tool_tinh_tong_dien_tich`. Hãy cộng theo diện tích đã lưu trong dữ liệu của từng thửa, không tính lại diện tích từ hình học. Nếu người dùng nêu rõ mã/tên các thửa (ví dụ T1 và T2 hoặc Thửa 1 và Thửa 2), truyền từng mã/tên vào `danh_sach_thua`. Nếu người dùng chỉ nêu số lượng (ví dụ "2 thửa") mà không xác định đó là thửa nào, hãy hỏi lại người dùng; không tự chọn thửa.
3. CÂU HỎI NGOÀI PHẠM VI HOẶC MƠ HỒ: Nếu người dùng hỏi các câu hỏi cảm tính, mơ hồ không có trong dữ liệu (ví dụ: "thửa nào đẹp nhất?", "giá lúa hôm nay?"), hãy lịch sự trả lời rằng: "Dữ liệu nông trại hiện tại chưa có thông tin về tiêu chí này. Hệ thống hiện chỉ quản lý các thông số kỹ thuật gồm loại cây trồng, diện tích, độ ẩm, nhiệt độ và khoảng cách vị trí."
4. PHONG CÁCH TRẢ LỜI: Ngắn gọn, thân thiện, chính xác, có định dạng rõ ràng (sử dụng icon phù hợp như 🌾, 💧, 📐, 🌡️, 🏠).
5. NHỚ NGỮ CẢNH: Khi có lịch sử hội thoại trong ngữ cảnh hiện tại, hãy dùng các kết quả đó để trả lời câu hỏi nối tiếp. Không suy đoán ngữ cảnh nếu lịch sử không được cung cấp.
6. Với câu hỏi có nhiều điều kiện (ví dụ cây trồng, ngưỡng độ ẩm và khoảng cách tới mốc), gọi `tool_loc_thua` một lần và truyền đủ điều kiện; các điều kiện được áp dụng đồng thời.
7. Khi công cụ trả về `parcel_ids`, hãy nêu đúng các thửa đó trong câu trả lời; API sẽ dùng các ID này để chọn thửa trên bản đồ.
8. Chỉ dùng dữ liệu do công cụ trả về; không hiển thị JSON thô. Nếu công cụ không có dữ liệu phù hợp, hãy nói rõ rằng chưa tìm thấy dữ liệu.
"""

def build_ai_tool_registry() -> list[types.FunctionDeclaration]:
    if client is None:
        return []

    return [
        types.FunctionDeclaration.from_callable(client=client, callable=fn)
        for fn in AI_TOOL_FUNCTIONS.values()
    ]


AI_TOOL_FUNCTIONS = {
    fn.__name__: fn
    for fn in (
        tool_loc_thua,
        tool_thua_cuc_tri,
        tool_tinh_tong_dien_tich,
        tool_thua_gan,
        tool_khoang_cach,
        tool_du_bao_thoi_tiet,
        tool_tra_cuu_gia_nong_san,
    )
}


AI_TOOL_REGISTRY = build_ai_tool_registry()
ai_config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=AI_TOOL_REGISTRY)] if AI_TOOL_REGISTRY else [],
    automatic_function_calling=types.AutomaticFunctionCallingConfig(
        disable=True,
    ),
)


GEMINI_REQUEST_TIMEOUT_SECONDS = 90
MAX_GEMINI_TOOL_ROUNDS = 10


def _response_parts(response) -> list[types.Part]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or [])


def _tool_result_parcel_ids(tool_result: str) -> list[str]:
    try:
        result = json.loads(tool_result)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(result, dict):
        return []
    parcel_ids = result.get("parcel_ids")
    if not isinstance(parcel_ids, list):
        return []
    return [str(parcel_id) for parcel_id in parcel_ids if parcel_id is not None]


def _build_gemini_history(history: list["ChatTurn"]) -> list[types.Content]:
    contents = []
    for turn in history[-12:]:
        text = turn.content
        if turn.role == "assistant" and turn.parcel_ids:
            text += "\n[Các thửa đã được công cụ xác định: " + ", ".join(turn.parcel_ids) + "]"
        contents.append(
            types.Content(
                role="user" if turn.role == "user" else "model",
                parts=[types.Part.from_text(text=text)],
            )
        )
    return contents


async def _send_gemini_message(chat_session, contents) -> tuple[str, list[str]]:
    deadline = asyncio.get_running_loop().time() + GEMINI_REQUEST_TIMEOUT_SECONDS
    parcel_ids = []

    for _ in range(MAX_GEMINI_TOOL_ROUNDS + 1):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Gemini chat timed out while processing tool calls.")

        response = await asyncio.wait_for(
            asyncio.to_thread(chat_session.send_message, contents),
            timeout=remaining,
        )
        parts = _response_parts(response)
        function_calls = [
            part.function_call
            for part in parts
            if getattr(part, "function_call", None)
            and getattr(part.function_call, "name", None)
        ]

        if not function_calls:
            text = "".join(
                part_text
                for part in parts
                if isinstance((part_text := getattr(part, "text", None)), str)
            ).strip()
            if not text:
                raise RuntimeError("Gemini returned neither text nor a function call.")
            return text, list(dict.fromkeys(parcel_ids))

        function_responses = []
        for function_call in function_calls:
            function_name = function_call.name
            function = AI_TOOL_FUNCTIONS.get(function_name)
            if function is None:
                logger.warning("Gemini requested an unknown tool: %s", function_name)
                tool_result = {"error": f"Tool '{function_name}' is not available."}
            else:
                try:
                    result = function(**dict(function_call.args or {}))
                    parcel_ids.extend(_tool_result_parcel_ids(result))
                    tool_result = {
                        "result": result
                    }
                except HTTPException as error:
                    tool_result = {"error": str(error.detail)}
                except Exception:
                    logger.exception("Gemini tool execution failed: tool=%s", function_name)
                    tool_result = {"error": "Tool execution failed."}

            function_responses.append(
                types.Part.from_function_response(
                    name=function_name,
                    response=tool_result,
                )
            )

        contents = function_responses

    raise RuntimeError("Gemini exceeded the maximum number of tool-call rounds.")


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)
    parcel_ids: list[str] = Field(default_factory=list, max_length=20)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: Optional[int] = None
    image_base64: Optional[str] = None
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)

@app.post("/api/chat", summary="Chat AI Gemini có xử lý công cụ GIS")
async def chat_endpoint(
    request: ChatRequest,
    db: Session = Depends(get_db),
    current_user: Optional[models.User] = Depends(get_optional_current_user)
):
    chat_history = models.ChatHistory(
        user_id=current_user.id if current_user else None,
        user_message=request.message,
        bot_response="Đang xử lý câu hỏi.",
        intent="GEMINI_PENDING",
    )
    db.add(chat_history)
    try:
        db.commit()
        db.refresh(chat_history)
    except Exception as error:
        db.rollback()
        logger.exception("Không thể lưu câu hỏi chat vào SSMS.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Không thể lưu câu hỏi vào SSMS nên yêu cầu chat chưa được xử lý.",
        ) from error

    if client is None:
        detail = "Backend chưa nhận được cấu hình GEMINI_API_KEY. Hãy đặt biến môi trường rồi khởi động lại server."
        finish_chat_history(
            db,
            chat_history,
            response=detail,
            intent="GEMINI_ERROR",
        )
        record_ai_error(
            db,
            user_id=current_user.id if current_user else None,
            model_name=None,
            status_code=None,
            error_type="GeminiConfigurationError",
            error_message="Backend chưa nhận được cấu hình GEMINI_API_KEY.",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        )
    
    contents = []
    if request.image_base64:
        try:
            image_data = request.image_base64
            mime_type = "image/jpeg"
            if "," in image_data:
                header, image_data = image_data.split(",", 1)
                if header.startswith("data:") and ";base64" in header:
                    mime_type = header[5:header.index(";")]
            contents.append(types.Part.from_bytes(
                data=base64.b64decode(image_data, validate=True),
                mime_type=mime_type,
            ))
        except (ValueError, base64.binascii.Error) as error:
            finish_chat_history(
                db,
                chat_history,
                response="Ảnh gửi lên không phải dữ liệu Base64 hợp lệ.",
                intent="GEMINI_ERROR",
            )
            raise HTTPException(status_code=422, detail="Ảnh gửi lên không phải dữ liệu Base64 hợp lệ.") from error
    contents.append(request.message)

    reply_text = None
    selected_parcel_ids = []
    last_error_status = None
    last_error_type = None
    last_error_message = None
    last_error_model = None
    stop_fallback = False
    for model_name in GEMINI_MODELS:
        for attempt in range(2):
            try:
                chat_session = client.chats.create(
                    model=model_name,
                    config=ai_config,
                    history=_build_gemini_history(request.history),
                )
                reply_text, selected_parcel_ids = await _send_gemini_message(chat_session, contents)
                break
            except Exception as e:
                error_code = getattr(e, "code", None) or getattr(e, "status_code", None)
                try:
                    last_error_status = int(error_code)
                except (TypeError, ValueError):
                    last_error_status = None
                last_error_type = type(e).__name__
                last_error_message = str(e)
                last_error_model = model_name
                logger.warning(
                    "Gemini request failed: model=%s status=%s error_type=%s",
                    model_name,
                    last_error_status or "unknown",
                    type(e).__name__,
                )
                if last_error_status in (401, 403):
                    detail = "Gemini từ chối API key hoặc quyền truy cập. Kiểm tra key, trạng thái key và quyền dùng Gemini API."
                    finish_chat_history(
                        db,
                        chat_history,
                        response=detail,
                        intent="GEMINI_ERROR",
                    )
                    record_ai_error(
                        db,
                        user_id=current_user.id if current_user else None,
                        model_name=model_name,
                        status_code=last_error_status,
                        error_type=last_error_type,
                        error_message=last_error_message,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=detail,
                    ) from e
                if isinstance(e, TimeoutError):
                    last_error_type = "TimeoutError"
                    last_error_message = str(e) or "Gemini request timed out."
                    stop_fallback = True
                    break
                if last_error_status in (500, 503) or "UNAVAILABLE" in type(e).__name__.upper():
                    if attempt == 0:
                        await asyncio.sleep(2)
                else:
                    break
        if reply_text:
            break
        if stop_fallback:
            break

    if not reply_text:
        record_ai_error(
            db,
            user_id=current_user.id if current_user else None,
            model_name=last_error_model,
            status_code=last_error_status,
            error_type=last_error_type or "GeminiRequestError",
            error_message=last_error_message or "Gemini không trả về phản hồi.",
        )
        if last_error_status == 404:
            detail = "Model Gemini không tồn tại hoặc không được bật cho key này. Kiểm tra GEMINI_MODEL và quyền truy cập model."
        elif last_error_status == 429:
            detail = "Gemini đã vượt hạn mức hoặc giới hạn tốc độ của API key."
        elif last_error_status in (500, 503):
            detail = "Dịch vụ Gemini đang tạm thời gặp sự cố. Hãy thử lại sau."
        elif last_error_type == "TimeoutError":
            detail = "Gemini phản hồi quá thời gian cho phép. Vui lòng thử lại sau."
        else:
            detail = "Không gọi được Gemini. Kiểm tra log backend để xem model và mã lỗi; API key không được ghi vào log."
        finish_chat_history(
            db,
            chat_history,
            response=detail,
            intent="GEMINI_ERROR",
        )
        response_status = (
            status.HTTP_504_GATEWAY_TIMEOUT
            if last_error_type == "TimeoutError"
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=response_status, detail=detail)

    finish_chat_history(
        db,
        chat_history,
        response=reply_text,
        intent="GEMINI_GIS_CHAT",
    )

    return {"reply": reply_text, "parcel_ids": selected_parcel_ids}


# Dữ liệu thửa đất dùng cho bản đồ
@app.get("/api/parcels/geojson", summary="Lấy ranh giới thửa đất để hiển thị bản đồ")
def get_parcels_geojson(db: Session = Depends(get_db)):
    return parcel_feature_collection(db)


# Các API GIS trực tiếp
@app.get("/api/loc-thua")
def api_loc_thua(
    dieu_kien: Optional[str] = Query(default=None),
    cay_trong: Optional[str] = Query(default=None),
    do_am_toi_thieu: Optional[float] = Query(default=None, ge=0, le=100),
    do_am_toi_da: Optional[float] = Query(default=None, ge=0, le=100),
    gan_doi_tuong: Optional[str] = Query(default=None),
    ban_kinh_m: Optional[float] = Query(default=None, ge=0),
):
    return gis_loc_thua(
        dieu_kien=dieu_kien,
        cay_trong=cay_trong,
        do_am_toi_thieu=do_am_toi_thieu,
        do_am_toi_da=do_am_toi_da,
        gan_doi_tuong=gan_doi_tuong,
        ban_kinh_m=ban_kinh_m,
    )

@app.get("/api/thua-cuc-tri")
def api_thua_cuc_tri(truong: str = "dien_tich", loai: str = "max"): 
    return gis_thua_cuc_tri(truong, loai)

@app.get("/api/tong-dien-tich", summary="Tính tổng diện tích các thửa đất")
def api_tinh_tong_dien_tich(
    dieu_kien: Optional[str] = Query(default=None, max_length=100),
    danh_sach_thua: Optional[list[str]] = Query(default=None),
):
    return gis_tinh_tong_dien_tich(
        dieu_kien=dieu_kien,
        danh_sach_thua=danh_sach_thua,
    )

@app.get("/api/thua-gan")
def api_thua_gan(lat: float, lon: float, ban_kinh_m: float = 500.0): 
    return gis_thua_gan(lat, lon, ban_kinh_m)

@app.get("/api/khoang-cach")
def api_khoang_cach(dt1: str, dt2: str): 
    return gis_khoang_cach(dt1, dt2)


# ------------------------------------------------------------------------------
# 6. ROUTER DÀNH RIÊNG CHO ADMIN
# ------------------------------------------------------------------------------
admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])

@admin_router.get("/parcels")
def get_admin_parcels(
    q: Optional[str] = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin),
):
    query = db.query(models.Parcel)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            (models.Parcel.parcel_code.ilike(term))
            | (models.Parcel.name.ilike(term))
            | (models.Parcel.crop_type.ilike(term))
        )
    parcels = query.order_by(models.Parcel.id.asc()).all()
    return {"total": len(parcels), "data": [parcel_to_feature(parcel) | {"db_id": parcel.id} for parcel in parcels]}


@admin_router.post("/parcels", status_code=status.HTTP_201_CREATED)
def create_parcel(
    data: ParcelCreateSchema,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin),
):
    try:
        geometry = validate_parcel_geometry(data.geometry)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    code = data.parcel_code.strip()
    if not code:
        raise HTTPException(status_code=422, detail="Mã thửa không được để trống.")
    if db.query(models.Parcel).filter(models.Parcel.parcel_code == code).first():
        raise HTTPException(status_code=409, detail="Mã thửa đã tồn tại.")

    parcel = models.Parcel(
        parcel_code=code,
        name=data.name.strip(),
        crop_type=data.crop_type.strip(),
        geometry_geojson=json.dumps(geometry, ensure_ascii=False),
        area_sqm=parcel_area_sqm(geometry),
        moisture=data.moisture,
        temperature=data.temperature,
        notes=data.notes.strip() if data.notes else None,
    )
    db.add(parcel)
    try:
        db.commit()
        db.refresh(parcel)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Mã thửa đã tồn tại.") from error
    refresh_gis_cache(db)
    return parcel_to_feature(parcel) | {"db_id": parcel.id}


@admin_router.put("/parcels/{parcel_id}")
def update_parcel(
    parcel_id: int,
    data: ParcelUpdateSchema,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin),
):
    parcel = db.query(models.Parcel).filter(models.Parcel.id == parcel_id).first()
    if not parcel:
        raise HTTPException(status_code=404, detail="Không tìm thấy thửa đất.")

    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Không có thông tin nào được gửi để cập nhật.")

    if "parcel_code" in updates:
        if updates["parcel_code"] is None:
            raise HTTPException(status_code=422, detail="Mã thửa không được để trống.")
        code = updates["parcel_code"].strip()
        if not code:
            raise HTTPException(status_code=422, detail="Mã thửa không được để trống.")
        duplicate = db.query(models.Parcel).filter(
            models.Parcel.parcel_code == code,
            models.Parcel.id != parcel_id,
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="Mã thửa đã tồn tại.")
        parcel.parcel_code = code

    for field in ("name", "crop_type"):
        if field in updates:
            if updates[field] is None or not updates[field].strip():
                raise HTTPException(status_code=422, detail=f"{field} không được để trống.")
            setattr(parcel, field, updates[field].strip())

    if "geometry" in updates:
        if updates["geometry"] is None:
            raise HTTPException(status_code=422, detail="Ranh giới không được để trống.")
        try:
            geometry = validate_parcel_geometry(updates["geometry"])
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        parcel.geometry_geojson = json.dumps(geometry, ensure_ascii=False)
        parcel.area_sqm = parcel_area_sqm(geometry)

    for field in ("moisture", "temperature", "notes"):
        if field in updates:
            value = updates[field]
            setattr(parcel, field, value.strip() if field == "notes" and value else value)

    try:
        db.commit()
        db.refresh(parcel)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Mã thửa đã tồn tại.") from error
    refresh_gis_cache(db)
    return parcel_to_feature(parcel) | {"db_id": parcel.id}


@admin_router.delete("/parcels/{parcel_id}")
def delete_parcel(
    parcel_id: int,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin),
):
    parcel = db.query(models.Parcel).filter(models.Parcel.id == parcel_id).first()
    if not parcel:
        raise HTTPException(status_code=404, detail="Không tìm thấy thửa đất.")
    db.delete(parcel)
    db.commit()
    refresh_gis_cache(db)
    return {"message": "Đã xóa thửa đất."}


@admin_router.get("/history")
def get_all_admin_chat_history(
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin)
):
    chats = db.query(models.ChatHistory).order_by(models.ChatHistory.id.desc()).all()
    return {"total": len(chats), "data": chats}

@admin_router.get("/summary")
def get_admin_summary(
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin),
):
    parcels = db.query(models.Parcel).order_by(models.Parcel.id.asc()).all()
    total_parcels = len(parcels)
    total_area_sqm = round(sum(float(parcel.area_sqm or 0) for parcel in parcels), 2)
    active_users = db.query(models.User).filter(models.User.is_active.is_(True)).count()
    recent_chats = db.query(models.ChatHistory).count()
    latest_error = db.query(models.AIErrorLog).order_by(models.AIErrorLog.created_at.desc()).first()

    summary = {
        "total_parcels": total_parcels,
        "total_area_sqm": total_area_sqm,
        "active_users": active_users,
        "chat_count": recent_chats,
        "latest_error": {
            "error_type": latest_error.error_type,
            "error_message": latest_error.error_message,
            "created_at": latest_error.created_at.isoformat() if latest_error and latest_error.created_at else None,
        } if latest_error else None,
        "stats": parcel_summary_stats(db),
    }
    return summary

@admin_router.delete("/history/{chat_id}")
def delete_admin_chat_history(
    chat_id: int,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin)
):
    chat = db.query(models.ChatHistory).filter(models.ChatHistory.id == chat_id).first()
    if not chat:
        raise HTTPException(status_code=404, detail="Không tìm thấy bản ghi chat.")
    db.delete(chat)
    db.commit()
    return {"message": "Xóa bản ghi lịch sử chat thành công"}

@admin_router.get("/users")
def get_all_users(
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin)
):
    users = db.query(models.User).order_by(models.User.id.asc()).all()
    return [{"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "created_at": u.created_at} for u in users]

@admin_router.put("/users/{user_id}/grant-admin")
def grant_admin_role(
    user_id: int,
    db: Session = Depends(get_db),
    current_admin: models.User = Depends(get_current_admin)
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Không tìm thấy người dùng này!")
    
    user.role = "admin"
    db.commit()
    return {"message": f"Đã nâng cấp quyền Admin cho tài khoản {user.username}!"}

app.include_router(admin_router)

# Mount các file giao diện tĩnh (HTML, CSS, JS)
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")


# ------------------------------------------------------------------------------
# 7. KHỞI CHẠY APPLICATION SERVER
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
