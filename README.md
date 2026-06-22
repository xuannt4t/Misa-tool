# MISA Auto Tool

Ứng dụng desktop Python mở MISA meInvoice bằng Google Chrome thật và giữ phiên đăng nhập của profile Chrome Windows hiện tại.

## Cài đặt và chạy development

Yêu cầu Python 3.11.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python main.py
```

Nhấn **Mở MISA**. Tool sẽ đóng toàn bộ cửa sổ Chrome đang mở, rồi khởi động lại Chrome bằng profile gần nhất của Windows để Playwright điều khiển được. Nếu MISA yêu cầu đăng nhập, hãy đăng nhập ngay trong cửa sổ trình duyệt; phiên đăng nhập vẫn nằm trong profile Chrome của Windows.

Chế độ này được điều khiển bởi `USE_WINDOWS_CHROME_PROFILE` trong `config/config.py`. Hiện đang là `True` để dùng session Chrome hiện tại. Đổi thành `False` để quay lại profile Playwright riêng tại `profile/default` (Chrome đang mở sẽ không bị đóng).

## Build EXE

Để tạo ZIP phát hành cho khách hàng và đăng lên GitHub Releases, xem [RELEASE.md](RELEASE.md). Lệnh build ZIP chạy được từ Command Prompt hoặc PowerShell:

```cmd
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1 -Version 1.0.0
```

```powershell
pyinstaller --onedir --windowed --name "MISA Auto Tool" main.py
```

Sau khi build, tạo các thư mục dữ liệu cạnh file EXE (nếu chúng chưa có):

```powershell
New-Item -ItemType Directory -Force "dist\MISA Auto Tool\profile", "dist\MISA Auto Tool\logs", "dist\MISA Auto Tool\data"
```

Cấu trúc phân phối:

```text
dist/
  MISA Auto Tool/
    MISA Auto Tool.exe
    profile/
    logs/
    data/
```

Playwright Chromium không được đóng gói vào EXE. Ứng dụng ưu tiên Google Chrome đã cài trên Windows; nếu không có sẽ dùng Chromium đã cài bằng `playwright install chromium` trên máy build/máy chạy.

## Sao lưu phiên đăng nhập

Đóng trình duyệt từ ứng dụng trước, sau đó sao chép profile Chrome Windows: `%LOCALAPPDATA%\Google\Chrome\User Data`. Khôi phục thư mục này trên máy mới để tiếp tục dùng phiên đăng nhập (nếu MISA chưa hết hạn phiên). Thư mục `profile` cạnh EXE chỉ là profile dự phòng khi không tìm thấy Chrome Windows.
