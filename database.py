import urllib.parse
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

# 1. Server Name và Database Name
SERVER_NAME = os.environ.get("SQLSERVER_HOST", r"DESKTOP-KNP80BR\MSSQLSERVER01")
DATABASE_NAME = os.environ.get("SQLSERVER_DATABASE", "AgricultureDB")
ODBC_DRIVER = os.environ.get("SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server")

# 2. Tạo chuỗi kết nối ODBC an toàn (Mã hóa tự động các ký tự đặc biệt như '\' và khoảng trắng)
params = urllib.parse.quote_plus(
    f"DRIVER={{{ODBC_DRIVER}}};"
    f"SERVER={SERVER_NAME};"
    f"DATABASE={DATABASE_NAME};"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

DATABASE_URL = f"mssql+pyodbc:///?odbc_connect={params}"

# 3. Khởi tạo Engine và Session
engine = create_engine(DATABASE_URL, echo=True) # echo=True để in log SQL ra terminal kiểm tra
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def migrate_chat_history_user_id():
    with engine.begin() as connection:
        connection.execute(text("""
            IF COL_LENGTH(N'dbo.chat_history', N'user_id') IS NULL
                ALTER TABLE dbo.chat_history ADD user_id INT NULL;

            IF OBJECT_ID(N'dbo.users', N'U') IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1
                   FROM sys.foreign_keys
                   WHERE name = N'FK_chat_history_users_user_id'
               )
                ALTER TABLE dbo.chat_history
                ADD CONSTRAINT FK_chat_history_users_user_id
                FOREIGN KEY (user_id) REFERENCES dbo.users(id);
        """))
