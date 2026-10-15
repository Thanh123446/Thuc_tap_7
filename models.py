from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

# Bảng 1: Lưu thông tin tài khoản và phân quyền (Admin / User)
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)  # Tên đăng nhập
    password_hash = Column(String(255), nullable=False)                      # Mật khẩu đã mã hóa
    full_name = Column(String(100), nullable=True)                           # Họ và tên
    role = Column(String(20), default="user", nullable=False)                # Vai trò: "admin" hoặc "user"
    is_active = Column(Boolean, default=True)                                # Trạng thái hoạt động
    created_at = Column(DateTime, default=datetime.utcnow)

    # Quan hệ liên kết tới lịch sử chat và nhật ký
    chat_histories = relationship("ChatHistory", back_populates="user")
    crop_logs = relationship("CropLog", back_populates="user")


# Bảng 2: Lưu lịch sử trò chuyện Chatbot
class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)         # ID người dùng (để trống nếu chat vô danh)
    user_message = Column(Text, nullable=False)                               # Câu hỏi người dùng
    bot_response = Column(Text, nullable=False)                               # AI trả lời
    intent = Column(String(100), nullable=True)                               # Ý định AI phân tích
    created_at = Column(DateTime, default=datetime.utcnow)

    # Liên kết ngược lại bảng User
    user = relationship("User", back_populates="chat_histories")


# Bảng 3: Lưu nhật ký ghi chú nông nghiệp
class CropLog(Base):
    __tablename__ = "crop_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)         # ID người tạo ghi chú
    so_thua = Column(String(50), index=True)                                  # Mã/số thửa đất
    ten_cay_trong = Column(String(100))                                       # Tên cây trồng
    ghi_chu = Column(Text, nullable=True)                                      # Ghi chú tình trạng
    ngay_tao = Column(DateTime, default=datetime.utcnow)

    # Liên kết ngược lại bảng User
    user = relationship("User", back_populates="crop_logs")


class Parcel(Base):
    __tablename__ = "parcels"

    id = Column(Integer, primary_key=True, index=True)
    parcel_code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    crop_type = Column(String(100), nullable=False)
    geometry_geojson = Column(Text, nullable=False)
    area_sqm = Column(Float, nullable=False)
    moisture = Column(Float, nullable=True)
    temperature = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class AIErrorLog(Base):
    __tablename__ = "ai_error_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    model = Column(String(100), nullable=True)
    status_code = Column(Integer, nullable=True)
    error_type = Column(String(200), nullable=False)
    error_message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)