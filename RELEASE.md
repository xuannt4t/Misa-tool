# Phát hành bản cập nhật

Mỗi bản cập nhật được phát hành dưới dạng ZIP trên GitHub Releases. ZIP chỉ chứa thư mục chương trình mới; không chứa dữ liệu, log hoặc profile của khách hàng.

## Tạo ZIP

Từ thư mục gốc dự án, chạy lệnh sau trong Command Prompt hoặc PowerShell:

```cmd
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1 -Version 1.0.0
```

File tạo ra là `release\MISA-Auto-Tool-v1.0.0.zip`.

## Đăng GitHub Releases

1. Vào repository GitHub, chọn **Releases** → **Draft a new release**.
2. Tạo tag, ví dụ `v1.0.0`.
3. Đính kèm file ZIP trong thư mục `release`.
4. Ghi thay đổi của phiên bản và bấm **Publish release**.

## Hướng dẫn cho máy khách

1. Đóng hoàn toàn MISA Auto Tool.
2. Sao lưu `data` và `profile` trong thư mục đang cài nếu cần.
3. Giải nén ZIP mới vào thư mục cha của `MISA Auto Tool`, chọn **Replace files** nếu Windows hỏi.
4. Không xóa thư mục `data`, `profile` hoặc `logs`; chúng chứa cấu hình, dữ liệu chạy và/hoặc phiên đăng nhập.
5. Mở lại `MISA Auto Tool.exe`.
