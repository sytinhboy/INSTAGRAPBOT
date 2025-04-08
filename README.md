# InstaGrap Bot 📥

InstaGrap là một bot Telegram giúp người dùng tải xuống nội dung từ Instagram, bao gồm bài đăng, video ngắn và stories. Được xây dựng bằng Python, bot cung cấp tính năng tải xuống chất lượng cao và giao diện dễ sử dụng. <a href="https://t.me/Instagramln_bot">InstaGrap Bot</a>

## Tính Năng 🌟

- Tải xuống bài đăng Instagram (ảnh đơn và album)
- Tải xuống Reels với chất lượng cao
- Tải xuống Stories (nếu có)
- Giao diện thân thiện với nút bấm
- Tự động dọn dẹp file sau khi gửi
- Hỗ trợ nhiều định dạng media
- Bao gồm caption và thông tin bài đăng

## Yêu Cầu Hệ Thống 📋

- Python 3.7 trở lên
- python-telegram-bot
- instagrapi
- python-dotenv
- requests

## Cài Đặt 🔧

1. Clone repository:
```bash
git clone https://github.com/sytinhboy/instagrap-bot.git
cd instagrap-bot
```

2. Cài đặt các gói cần thiết:
```bash
pip install -r requirements.txt
```

3. Tạo file `.env` trong thư mục gốc:
```bash
touch .env
```

4. Thêm thông tin đăng nhập vào file `.env`:
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
INSTAGRAM_USERNAME=your_instagram_username
INSTAGRAM_PASSWORD=your_instagram_password
```

5. Đặt quyền truy cập cho file `.env`:
```bash
chmod 600 .env
```

## Cách Sử Dụng 🚀

1. Khởi động bot:
```bash
python instagrap.py
```

2. Trong Telegram, gửi URL Instagram cho bot:
- URL bài đăng: `https://www.instagram.com/p/XXXX/`
- URL video ngắn: `https://www.instagram.com/reel/XXXX/`
- URL story: `https://www.instagram.com/stories/username/XXXX/`

## Triển Khai trên PythonAnywhere 🌐

1. Tải các file lên PythonAnywhere:
- Tải lên `instagrap.py`
- Tải lên file `.env`
- Tải lên `requirements.txt`

2. Cài đặt các gói phụ thuộc:
```bash
pip install -r requirements.txt
```

3. Thiết lập biến môi trường trong PythonAnywhere:
- Vào tab Web
- Thêm các biến môi trường:
  ```
  TELEGRAM_BOT_TOKEN=your_token
  INSTAGRAM_USERNAME=your_username
  INSTAGRAM_PASSWORD=your_password
  ```

4. Cấu hình web app và khởi động bot

## Lệnh Bot 📝

- `/start` - Khởi động bot
- `/help` - Hiển thị trợ giúp
- `/menu` - Hiển thị menu chính

## Đóng Góp 🤝

Chào đón mọi đóng góp, báo lỗi và yêu cầu tính năng!


## Tác Giả ✨

- GitHub: [@sytinhboy](https://github.com/sytinhboy)
- Telegram: [@sytinhboy](https://t.me/sytinhboy)

## Cảm Ơn 🙏

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [instagrapi](https://github.com/adw0rd/instagrapi)



