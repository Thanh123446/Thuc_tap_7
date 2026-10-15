from database import engine, Base
import models

try:
    print("-> Đang thử kết nối tới SQL Server...")
    # Thử kết nối và tự động tạo bảng
    Base.metadata.create_all(bind=engine)
    print(" SUCCESS: Kết nối SQL Server thành công! Bảng ChatHistory đã được tạo trong SSMS.")
except Exception as e:
    print(" LỖI KẾT NỐI SQL SERVER:")
    print(e)