import os
import logging
import requests
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext, CallbackQueryHandler
import json
import asyncio
import aiohttp
from urllib.parse import urlparse
import tempfile
import glob
import shutil
import time
from instagrapi import Client
from instagrapi import config as ig_config
from instagrapi.exceptions import (
    ClientJSONDecodeError,
    ClientNotFoundError,
    LoginRequired,
    MediaNotFound,
)
from dotenv import load_dotenv
import random
import time 

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
class Config:
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    INSTAGRAM_USERNAME = os.getenv('INSTAGRAM_USERNAME')
    INSTAGRAM_PASSWORD = os.getenv('INSTAGRAM_PASSWORD')
    DOWNLOAD_DIR = "instagram_downloads"
    
    @classmethod
    def validate(cls):
        missing = []
        if not cls.TOKEN:
            missing.append('TELEGRAM_BOT_TOKEN')
        if not cls.INSTAGRAM_USERNAME:
            missing.append('INSTAGRAM_USERNAME')
        if not cls.INSTAGRAM_PASSWORD:
            missing.append('INSTAGRAM_PASSWORD')
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}\n"
                           f"Please create a .env file with the following variables:\n"
                           f"TELEGRAM_BOT_TOKEN=your_token\n"
                           f"INSTAGRAM_USERNAME=your_username\n"
                           f"INSTAGRAM_PASSWORD=your_password")

# Instagram URL pattern
INSTAGRAM_URL_PATTERN = r'https?://(?:www\.)?instagram\.com/(?:p|reel|stories|s)/([^/?]+)(?:/([^/?]+))?'

# Thư mục lưu trữ
DOWNLOAD_DIR = Config.DOWNLOAD_DIR
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


class InstagramBotClient(Client):
    """
    Bỏ qua login_flow() mặc định (reels_tray + timeline) — Instagram thường trả 400
    và body rỗng trên challenge/, gây JSONDecodeError dù login 200 OK.
    """

    def login_flow(self) -> bool:
        return True


# Instagram client (timeout mặc định 1s của instagrapi dễ body không đủ → JSON lỗi)
cl = InstagramBotClient(request_timeout=30)
cl.delay_range = [2, 5]  # Delay giữa các request


def _is_json_parse_error(error: Exception) -> bool:
    """Detect empty/invalid JSON responses from Instagram API."""
    if isinstance(error, (json.JSONDecodeError, ClientJSONDecodeError)):
        return True
    message = str(error).lower()
    return "expecting value: line 1 column 1" in message or "jsondecodeerror" in message


async def fetch_media_info_resilient(media_pk: str, shortcode: str | None = None):
    """
    Instagram thường trả body rỗng; instagrapi bọc lỗi trong ClientJSONDecodeError.
    Thử lần lượt nhiều nguồn (v1, web a1, GQL có session, media_info kết hợp) + backoff.
    """
    pk = cl.media_pk(media_pk)
    delays = [1, 2, 4, 8]
    last_err = None

    def _strategies():
        if cl.user_id:
            yield "media_info_v1", lambda: cl.media_info_v1(pk)
        yield "media_info_a1", lambda: cl.media_info_a1(pk)
        if cl.user_id:

            def _gql_with_session():
                cl.inject_sessionid_to_public()
                return cl.media_info_gql(pk)

            yield "media_info_gql", _gql_with_session
        yield "media_info", lambda: cl.media_info(pk, use_cache=False)

    for round_i, delay in enumerate(delays):
        cl.inject_sessionid_to_public()
        for name, fn in _strategies():
            try:
                media = fn()
                if round_i:
                    logger.info(f"Đã lấy media_info qua {name} sau {round_i} vòng retry")
                return media
            except (MediaNotFound, ClientNotFoundError):
                raise
            except Exception as e:
                last_err = e
                logger.debug(f"{name} thất bại: {e}")
                try:
                    cl._medias_cache.pop(pk, None)
                except Exception:
                    pass
                continue
        if round_i < len(delays) - 1 and last_err is not None and _is_json_parse_error(last_err):
            logger.warning(
                f"Mọi nguồn media_info đều lỗi JSON (vòng {round_i + 1}/{len(delays)}), chờ {delay}s..."
            )
            await asyncio.sleep(delay)
            continue
        break

    if last_err is not None:
        raise last_err
    raise RuntimeError(f"Không lấy được media_info cho pk={pk} shortcode={shortcode!r}")


def _latest_instagrapi_app_version() -> str:
    """Phiên bản app Android mới nhất khai báo trong instagrapi (Meta chặn bản API quá cũ)."""
    return max(
        ig_config.APP_SETTINGS.keys(),
        key=lambda s: tuple(int(p) for p in s.split(".")),
    )


def sync_instagrapi_fingerprint(client: Client, reset_device: bool = False) -> None:
    """
    Căn chỉnh app_version / version_code / bloks_versioning_id / user_agent theo bản instagrapi.
    Session JSON cũ thường kèm fingerprint 269.x — dễ bị 400 + challenge; cần nâng lên bản hiện tại.
    """
    if reset_device:
        client.set_device({})
    client.set_app(_latest_instagrapi_app_version())
    client.set_user_agent()
    client.init()


def _best_photo_url(media_info) -> str | None:
    """Chọn URL ảnh có width lớn nhất nếu có; không gọi lại media_info (tránh GQL body rỗng)."""
    iv2 = getattr(media_info, "image_versions2", None)
    candidates = getattr(iv2, "candidates", None) if iv2 else None
    if candidates:
        best = max(candidates, key=lambda c: getattr(c, "width", 0) or 0)
        u = getattr(best, "url", None)
        if u:
            return str(u)
    thumb = getattr(media_info, "thumbnail_url", None)
    return str(thumb) if thumb else None


def init_instagram_client():
    """Initialize Instagram client with login and session management."""
    global cl
    try:
        Config.validate()
        session_file = "instagram_session.json"
        
        # Thử load session cũ trước
        if os.path.exists(session_file):
            try:
                cl.load_settings(session_file, override_app_version=True)
                sync_instagrapi_fingerprint(cl, reset_device=False)
                cl.account_info()  # Nhẹ hơn timeline/reels, tránh cold-start 400
                logger.info("✅ Đã load session cũ thành công")
                
                # Thêm delay để tránh rate limit
                time.sleep(2)
                return True
            except Exception as e:
                logger.warning(f"⚠️ Session cũ không hợp lệ: {e}")
                try:
                    os.remove(session_file)
                except:
                    pass
                if _is_json_parse_error(e):
                    # Reset client state if previous session check got invalid JSON response
                    cl = InstagramBotClient(request_timeout=30)
                    cl.delay_range = [2, 5]

        # Nếu không có session hoặc session hết hạn, đăng nhập lại
        try:
            # Không hard-code Instagram 269.x — dùng DEVICE_SETTINGS + APP_SETTINGS của instagrapi đã cài
            sync_instagrapi_fingerprint(cl, reset_device=True)
            cl.delay_range = [2, 5]
            logger.info(
                "📱 Đang dùng fingerprint instagrapi: app %s",
                _latest_instagrapi_app_version(),
            )

            # Thêm delay trước khi đăng nhập
            time.sleep(3)
            
            # Thực hiện đăng nhập
            login_response = cl.login(
                username=Config.INSTAGRAM_USERNAME,
                password=Config.INSTAGRAM_PASSWORD,
                relogin=True
            )

            if login_response:
                # Lưu session mới
                cl.dump_settings(session_file)
                logger.info("✅ Đăng nhập và lưu session mới thành công")
                
                # Thêm delay sau khi đăng nhập
                time.sleep(2)
                return True
            else:
                logger.error("❌ Đăng nhập thất bại")
                return False

        except Exception as e:
            if _is_json_parse_error(e):
                logger.warning("⚠️ Instagram trả về dữ liệu không hợp lệ, thử đăng nhập lại...")
                try:
                    if os.path.exists(session_file):
                        os.remove(session_file)
                except Exception:
                    pass

                # Re-create a clean client state and retry once
                cl = InstagramBotClient(request_timeout=30)
                cl.delay_range = [2, 5]
                try:
                    sync_instagrapi_fingerprint(cl, reset_device=True)
                    cl.delay_range = [2, 5]
                    time.sleep(3)
                    login_response = cl.login(
                        username=Config.INSTAGRAM_USERNAME,
                        password=Config.INSTAGRAM_PASSWORD,
                        relogin=True
                    )
                    if login_response:
                        cl.dump_settings(session_file)
                        logger.info("✅ Đăng nhập lại thành công sau khi reset client")
                        time.sleep(2)
                        return True
                except Exception as retry_error:
                    logger.error(f"❌ Đăng nhập lại thất bại: {retry_error}")
                return False
            if "challenge_required" in str(e):
                try:
                    logger.info("🔐 Yêu cầu xác minh bảo mật...")
                    
                    # Chọn phương thức xác minh (0: SMS, 1: Email)
                    choice = cl.challenge_resolve_choice()
                    if choice:
                        logger.info("✅ Đã chọn phương thức xác minh")
                    
                    # Nhập mã xác minh
                    code = input("📱 Nhập mã xác minh từ SMS/Email: ")
                    cl.challenge_resolve(code)
                    
                    # Lưu session sau khi xác minh
                    cl.dump_settings(session_file)
                    logger.info("✅ Xác minh và lưu session thành công")
                    return True
                    
                except Exception as challenge_error:
                    logger.error(f"❌ Lỗi xác minh: {challenge_error}")
                    return False
            elif "Please wait a few minutes before you try again" in str(e):
                logger.warning("⚠️ Đã bị rate limit, đợi 15 phút và thử lại sau")
                time.sleep(900)  # Đợi 15 phút
                return init_instagram_client()  # Thử lại sau khi đợi
            else:
                logger.error(f"❌ Lỗi đăng nhập: {e}")
                return False
                
    except Exception as e:
        logger.error(f"❌ Lỗi khởi tạo client: {e}")
        return False

async def download_instagram_content(shortcode: str) -> list:
    """Download Instagram content using instagrapi"""
    media_files = []
    try:
        # Thêm delay ngẫu nhiên trước khi bắt đầu request để tránh rate limit
        delay = random.uniform(3, 7)
        await asyncio.sleep(delay)
        
        # Get media ID from shortcode
        media_pk = cl.media_pk_from_code(shortcode)
        
        # Thêm delay giữa các request
        await asyncio.sleep(2)
        
        # Get media info
        try:
            media_info = await fetch_media_info_resilient(media_pk, shortcode)
        except Exception as e:
            if "Please wait a few minutes before you try again" in str(e):
                logger.warning("⚠️ Rate limit khi lấy media info, đợi 15 phút và thử lại")
                await asyncio.sleep(200)  # Đợi 3 phút
                return await download_instagram_content(shortcode)
            else:
                raise e
                
        username = media_info.user.username
        
        # Lấy caption và loại bỏ hashtag
        caption = media_info.caption_text if media_info.caption_text else "Không có caption"
        # Remove hashtags using regex
        caption = re.sub(r'#\w+', '', caption).strip()
        
        posted_at = media_info.taken_at.strftime("%H:%M %d/%m/%Y")
        
        # Tạo thông tin chi tiết về bài viết với nút bấm username
        post_info = (
            f"📝 Caption: {caption}\n\n"
            f"👤 Posted by: @{username}\n"
            f"🕒 Posted at: {posted_at}"
        )
        
        # Tạo keyboard với nút bấm username
        keyboard = [[InlineKeyboardButton(
            text=f"@{username}",
            url=f"https://instagram.com/{username}"
        )]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Create directory for this post
        target_dir = os.path.join(DOWNLOAD_DIR, shortcode)
        os.makedirs(target_dir, exist_ok=True)
        
        if media_info.media_type == 1:  # Photo
            try:
                # Thêm delay trước khi download
                await asyncio.sleep(2)
                
                # Không dùng photo_download(): bên trong gọi media_info() → GQL dễ JSON lỗi
                photo_url = _best_photo_url(media_info)
                if not photo_url:
                    raise RuntimeError("Không có URL ảnh trong media_info")
                fname = "{0}_{1}".format(username, media_pk)
                photo_path = cl.photo_download_by_url(photo_url, fname, target_dir)
                photo_path_str = str(photo_path)
                media_files.append({
                    "path": photo_path_str, 
                    "type": "image",
                    "username": username,  # Thêm username vào media_files
                    "media_info": media_info  # Thêm toàn bộ media_info
                })
            except Exception as e:
                if "Please wait a few minutes before you try again" in str(e):
                    logger.warning("⚠️ Rate limit khi tải ảnh, đợi 15 phút và thử lại")
                    await asyncio.sleep(200)  # Đợi 3 phút
                    return await download_instagram_content(shortcode)
                else:
                    raise e
            
        elif media_info.media_type == 2:  # Video
            try:
                # Thêm delay trước khi xử lý video
                await asyncio.sleep(3)
                
                # Lấy URL video chất lượng cao nhất từ resources
                video_url = None
                max_width = 0
                
                # Kiểm tra resources trước
                if hasattr(media_info, 'resources') and media_info.resources:
                    for resource in media_info.resources:
                        if hasattr(resource, 'video_url') and resource.video_url:
                            if hasattr(resource, 'width') and resource.width > max_width:
                                video_url = resource.video_url
                                max_width = resource.width
                
                # Nếu không có trong resources, thử lấy từ video_versions
                if not video_url and hasattr(media_info, 'video_versions'):
                    for version in media_info.video_versions:
                        if version.width > max_width:
                            video_url = version.url
                            max_width = version.width
                
                # Fallback to default video_url if still not found
                if not video_url:
                    video_url = media_info.video_url
                
                if video_url:
                    file_name = f"{shortcode}.mp4"
                    file_path = os.path.join(target_dir, file_name)
                    
                    # Sử dụng aiohttp để tải video bất đồng bộ
                    async with aiohttp.ClientSession() as session:
                        async with session.get(video_url, timeout=60) as response:
                            if response.status == 200:
                                with open(file_path, 'wb') as f:
                                    f.write(await response.read())
                                media_files.append({
                                    "path": file_path, 
                                    "type": "video",
                                    "username": username,
                                    "media_info": media_info,
                                    "quality": f"{max_width}p"  # Thêm thông tin độ phân giải
                                })
                                logger.info(f"Đã tải video chất lượng cao {max_width}p: {file_path}")
                            else:
                                raise Exception(f"Không thể tải video (HTTP {response.status})")
                else:
                    raise Exception("Không tìm thấy URL video chất lượng cao")
                    
            except Exception as e:
                if "Please wait a few minutes before you try again" in str(e):
                    logger.warning("⚠️ Rate limit khi tải video, đợi 15 phút và thử lại")
                    await asyncio.sleep(200)  # Đợi 3 phút
                    return await download_instagram_content(shortcode)
                
                logger.error(f"Lỗi khi tải video chất lượng cao: {e}")
                # Fallback: sử dụng phương thức tải thông thường
                try:
                    await asyncio.sleep(3)  # Thêm delay trước khi thử lại
                    if not media_info.video_url:
                        raise RuntimeError("Không có video_url trong media_info")
                    video_path = cl.video_download_by_url(
                        str(media_info.video_url),
                        "{0}_{1}".format(username, media_pk),
                        target_dir,
                    )
                    if video_path and os.path.exists(str(video_path)):
                        new_path = os.path.join(target_dir, f"{shortcode}.mp4")
                        os.rename(str(video_path), new_path)
                        media_files.append({
                            "path": new_path,
                            "type": "video",
                            "username": username,
                            "media_info": media_info
                        })
                        logger.info(f"Đã tải video dự phòng: {new_path}")
                except Exception as backup_error:
                    if "Please wait a few minutes before you try again" in str(backup_error):
                        logger.warning("⚠️ Rate limit khi tải video dự phòng, đợi 15 phút và thử lại")
                        await asyncio.sleep(200)  # Đợi 3 phút
                        return await download_instagram_content(shortcode)
                    logger.error(f"Lỗi khi tải video dự phòng: {backup_error}")
            
        elif media_info.media_type == 8:  # Album
            try:
                # Thêm delay trước khi download album
                await asyncio.sleep(3)
                
                # Không dùng album_download(): bên trong gọi media_info() lại
                album_files = []
                for resource in media_info.resources:
                    fn = f"{media_info.user.username}_{resource.pk}"
                    if resource.media_type == 1:
                        album_files.append(
                            cl.photo_download_by_url(
                                str(resource.thumbnail_url), fn, target_dir
                            )
                        )
                    elif resource.media_type == 2:
                        album_files.append(
                            cl.video_download_by_url(
                                str(resource.video_url), fn, target_dir
                            )
                        )
                    else:
                        raise RuntimeError(
                            f"Kiểu media album không hỗ trợ: {resource.media_type}"
                        )
                
                # Thêm delay giữa các file trong album
                await asyncio.sleep(2)
                
                for file_path in album_files:
                    file_path_str = str(file_path)
                    if file_path_str.endswith('.mp4'):
                        # Thử tải lại video với chất lượng cao
                        try:
                            video_url = media_info.video_url
                            if video_url:
                                # Sử dụng aiohttp để tải video bất đồng bộ
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(video_url, timeout=60) as response:
                                        if response.status == 200:
                                            with open(file_path_str, 'wb') as f:
                                                f.write(await response.read())
                                            logger.info(f"Đã tải lại video album với chất lượng cao: {file_path_str}")
                        except Exception as e:
                            if "Please wait a few minutes before you try again" in str(e):
                                # Không cần thử lại cả album, chỉ ghi log
                                logger.warning(f"⚠️ Rate limit khi tải lại video album, bỏ qua: {file_path_str}")
                            else:
                                logger.error(f"Không thể tải lại video album chất lượng cao: {e}")
                        
                        media_files.append({
                            "path": file_path_str, 
                            "type": "video",
                            "username": username,
                            "media_info": media_info
                        })
                    else:
                        media_files.append({
                            "path": file_path_str, 
                            "type": "image",
                            "username": username,
                            "media_info": media_info
                        })
            except Exception as e:
                if "Please wait a few minutes before you try again" in str(e):
                    logger.warning("⚠️ Rate limit khi tải album, đợi 15 phút và thử lại")
                    await asyncio.sleep(200)  # Đợi 3 phút
                    return await download_instagram_content(shortcode)
                else:
                    raise e
        
        logger.info(f"Downloaded {len(media_files)} files from {shortcode}")
        
        # Verify all files exist and are not empty
        valid_files = []
        for media_file in media_files:
            file_path = media_file["path"]
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                valid_files.append(media_file)
                logger.info(f"Verified file: {file_path}")
            else:
                logger.error(f"Invalid or empty file: {file_path}")
        
        # Thêm thông tin bài viết vào media_files đầu tiên
        if valid_files:
            valid_files[0]["post_info"] = post_info
        
        return valid_files
    
    except LoginRequired:
        logger.error("Login required, trying to re-login")
        if init_instagram_client():
            # Thêm delay trước khi thử lại
            await asyncio.sleep(5)
            # Retry once after re-login
            return await download_instagram_content(shortcode)
        return []
    except Exception as e:
        if "Please wait a few minutes before you try again" in str(e):
            logger.warning("⚠️ Rate limit ở cấp độ function, đợi 15 phút và thử lại")
            await asyncio.sleep(200)  # Đợi 3 phút
            return await download_instagram_content(shortcode)
            
        logger.error(f"Error downloading content: {e}")
        return []

async def download_instagram_story(username: str, story_id: str = None) -> list:
    """Download Instagram story using instagrapi"""
    media_files = []
    processed_ids = set()
    try:
        # Lấy user ID từ username
        user_id = cl.user_id_from_username(username)
        
        # Tạo thư mục cho stories
        target_dir = os.path.join(DOWNLOAD_DIR, f"stories_{username}")
        os.makedirs(target_dir, exist_ok=True)
        
        # Lấy danh sách stories
        stories = cl.user_stories(user_id)
        
        if not stories:
            logger.error(f"Không tìm thấy story nào của {username}")
            return []
        
        logger.info(f"Tìm thấy {len(stories)} story của {username}")
        
        # Sắp xếp stories theo thời gian để tải theo thứ tự
        sorted_stories = sorted(stories, key=lambda x: x.taken_at)
        
        for story in sorted_stories:
            # Nếu có story_id cụ thể, chỉ tải story đó
            if story_id and str(story.pk) != story_id:
                continue
                
            # Kiểm tra story ID đã xử lý chưa
            if story.pk in processed_ids:
                logger.info(f"Story {story.pk} đã được xử lý trước đó")
                continue
                
            processed_ids.add(story.pk)
            
            try:
                timestamp = story.taken_at.strftime("%Y%m%d_%H%M%S")
                
                if story.media_type == 1:  # Photo
                    file_name = f"story_{username}_{timestamp}_{story.pk}.jpg"
                    file_path = os.path.join(target_dir, file_name)
                    
                    if os.path.exists(file_path):
                        logger.info(f"File {file_name} đã tồn tại, bỏ qua")
                        continue
                    
                    # Lấy URL chất lượng cao nhất cho ảnh
                    photo_url = story.thumbnail_url_info()[-1]['url']
                    response = requests.get(photo_url)
                    if response.status_code == 200:
                        with open(file_path, 'wb') as f:
                            f.write(response.content)
                        media_files.append({
                            "path": file_path,
                            "type": "image",
                            "taken_at": story.taken_at,
                            "username": username
                        })
                        logger.info(f"Đã tải story ảnh chất lượng cao: {story.pk} - {file_name}")
                    
                elif story.media_type == 2:  # Video
                    file_name = f"story_{username}_{timestamp}_{story.pk}.mp4"
                    file_path = os.path.join(target_dir, file_name)
                    
                    if os.path.exists(file_path):
                        logger.info(f"File {file_name} đã tồn tại, bỏ qua")
                        continue
                    
                    # Lấy URL video chất lượng cao nhất
                    video_url = story.video_url
                    response = requests.get(video_url)
                    if response.status_code == 200:
                        with open(file_path, 'wb') as f:
                            f.write(response.content)
                        media_files.append({
                            "path": file_path,
                            "type": "video",
                            "taken_at": story.taken_at,
                            "username": username
                        })
                        logger.info(f"Đã tải story video chất lượng cao: {story.pk} - {file_name}")
                    
            except Exception as e:
                logger.error(f"Lỗi khi tải story {story.pk}: {e}")
                # Nếu tải chất lượng cao thất bại, thử tải bằng phương thức thông thường
                try:
                    story_path = cl.story_download(story.pk, folder=target_dir)
                    if story_path and os.path.exists(str(story_path)):
                        new_path = os.path.join(target_dir, file_name)
                        os.rename(str(story_path), new_path)
                        media_files.append({
                            "path": new_path,
                            "type": "video" if story.media_type == 2 else "image",
                            "taken_at": story.taken_at,
                            "username": username
                        })
                        logger.info(f"Đã tải story dự phòng: {story.pk} - {file_name}")
                except Exception as backup_error:
                    logger.error(f"Lỗi khi tải story dự phòng {story.pk}: {backup_error}")
                continue
        
        # Sắp xếp media_files theo thời gian đăng
        media_files.sort(key=lambda x: x["taken_at"])
        
        # Kiểm tra và xác thực các file đã tải
        valid_files = []
        for media_file in media_files:
            file_path = media_file["path"]
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                valid_files.append(media_file)
                logger.info(f"Đã xác thực file: {file_path}")
            else:
                logger.error(f"File không hợp lệ hoặc rỗng: {file_path}")
        
        return valid_files
        
    except Exception as e:
        logger.error(f"Lỗi khi tải stories: {e}")
        return []

async def process_instagram_url(update: Update, context: CallbackContext) -> None:
    """Process Instagram URL and send media."""
    url = update.message.text.strip()
    
    if not re.match(INSTAGRAM_URL_PATTERN, url):
        await update.message.reply_text("Vui lòng gửi URL Instagram hợp lệ.")
        return
    
    processing_message = await update.message.reply_text("⌛ Đang xử lý...")
    
    try:
        # Extract information from URL
        match = re.search(INSTAGRAM_URL_PATTERN, url)
        first_part = match.group(1)
        second_part = match.group(2) if match.group(2) else None
        
        await processing_message.edit_text("🔍 Đang kiểm tra URL...")
        
        # Xác định loại nội dung và tải xuống
        if 'stories' in url or '/s/' in url:
            # URL là story
            username = first_part
            story_id = second_part
            await processing_message.edit_text(f"📥 Đang tải story của @{username}...")
            media_items = await download_instagram_story(username, story_id)
            
            if media_items:
                await processing_message.edit_text(f"✅ Đã tìm thấy {len(media_items)} story từ @{username}\n⌛ Đang chuẩn bị gửi...")
            else:
                await processing_message.edit_text(f"⚠️ Không tìm thấy story nào từ @{username}")
                return
        else:
            # URL là post hoặc reel bình thường
            shortcode = first_part
            await processing_message.edit_text("📥 Đang tải nội dung...")
            media_items = await download_instagram_content(shortcode)
        
        if not media_items:
            await processing_message.edit_text("⚠️ Không thể tải xuống. Nguyên nhân có thể:\n"
                                            "• Bài viết đã bị xóa\n"
                                            "• Tài khoản riêng tư\n"
                                            "• Story đã hết hạn\n"
                                            "• Instagram đang giới hạn truy cập")
            return
        
        # Gửi thông tin bài viết với nút bấm
        if media_items and "post_info" in media_items[0]:
            keyboard = [[InlineKeyboardButton(
                text=f"@{media_items[0]['username']}",
                url=f"https://instagram.com/{media_items[0]['username']}"
            )]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                media_items[0]["post_info"],
                reply_markup=reply_markup
            )
        
        await processing_message.edit_text(f"📤 Đang gửi {len(media_items)} file...")
        
        success_videos = 0
        success_images = 0
        processed_files = set()  # Lưu trữ đường dẫn các file đã xử lý
            
        for i, media_item in enumerate(media_items):
            try:
                file_path = media_item["path"]
                media_type = media_item["type"]
                
                logger.info(f"Xử lý file {i+1}/{len(media_items)}")
                logger.info(f"Đường dẫn: {file_path}")
                logger.info(f"Loại file: {media_type}")
                
                if not os.path.exists(file_path):
                    logger.error(f"File không tồn tại: {file_path}")
                    continue
                
                file_size = os.path.getsize(file_path)
                if file_size == 0:
                    logger.error(f"File rỗng: {file_path}")
                    continue
                
                logger.info(f"Bắt đầu gửi file {file_path} (size: {file_size} bytes)")
                
                try:
                    with open(file_path, 'rb') as file:
                        # Tạo tên file với username, shortcode và số thứ tự
                        if 'stories' in file_path:
                            # Đối với story, lấy username và tính thời gian đã đăng
                            username = media_item.get("username", "unknown")
                            taken_at = media_item.get("taken_at")
                            
                            # Tính số giờ đã trôi qua
                            time_diff = time.time() - taken_at.timestamp()
                            hours_ago = int(time_diff / 3600)
                            
                            # Tạo chuỗi thời gian
                            if hours_ago == 0:
                                time_str = "Just now"
                            elif hours_ago == 1:
                                time_str = "1H ago"
                            else:
                                time_str = f"{hours_ago}H ago"
                            
                            # Chỉ thêm số thứ tự nếu có nhiều story
                            if len(media_items) > 1:
                                filename = f"story_{i+1}.{'mp4' if media_type == 'video' else 'jpg'}"
                                caption = f"{media_type.capitalize()} {i+1}/{len(media_items)}\n🕒 {time_str}"
                            else:
                                filename = f"story.{'mp4' if media_type == 'video' else 'jpg'}"
                                caption = f"{media_type.capitalize()}\n🕒 {time_str}"
                            
                            # Gửi file với caption
                            if media_type == "video":
                                await update.message.reply_document(
                                    document=file,
                                    filename=filename,
                                    caption=caption,
                                    read_timeout=300,
                                    write_timeout=300,
                                    connect_timeout=60,
                                    disable_content_type_detection=True
                                )
                                success_videos += 1
                                processed_files.add(file_path)  # Add to processed files for deletion
                                logger.info(f"Đã gửi thành công video {i+1}")
                            else:  # image
                                await update.message.reply_document(
                                    document=file,
                                    filename=filename,
                                    caption=caption,
                                    read_timeout=120,
                                    write_timeout=120,
                                    connect_timeout=60
                                )
                                success_images += 1
                                processed_files.add(file_path)  # Add to processed files for deletion
                                logger.info(f"Đã gửi thành công ảnh {i+1}")
                        else:
                            # Đối với post thường
                            username = media_item.get("username", "unknown")
                            shortcode = first_part
                            if len(media_items) > 1:
                                filename = f"{shortcode}_{i+1}.{'mp4' if media_type == 'video' else 'jpg'}"
                                caption = f"{media_type.capitalize()} {i+1}/{len(media_items)}"
                            else:
                                filename = f"{shortcode}.{'mp4' if media_type == 'video' else 'jpg'}"
                                caption = f"{media_type.capitalize()}"
                            
                            # Gửi file với caption
                            if media_type == "video":
                                await update.message.reply_document(
                                    document=file,
                                    filename=filename,
                                    caption=caption,
                                    read_timeout=300,
                                    write_timeout=300,
                                    connect_timeout=60,
                                    disable_content_type_detection=True
                                )
                                success_videos += 1
                                processed_files.add(file_path)  # Add to processed files for deletion
                                logger.info(f"Đã gửi thành công video {i+1}")
                            else:  # image
                                await update.message.reply_document(
                                    document=file,
                                    filename=filename,
                                    caption=caption,
                                    read_timeout=120,
                                    write_timeout=120,
                                    connect_timeout=60
                                )
                                success_images += 1
                                processed_files.add(file_path)  # Add to processed files for deletion
                                logger.info(f"Đã gửi thành công ảnh {i+1}")
                except Exception as send_error:
                    logger.error(f"Lỗi khi gửi file {file_path}: {send_error}")
            except Exception as e:
                logger.error(f"Lỗi khi xử lý file {i+1}: {e}")
        
        # Xóa các file đã gửi thành công
        for file_path in processed_files:
            try:
                os.remove(file_path)
                logger.info(f"Đã xóa file: {file_path}")
            except Exception as e:
                logger.error(f"Lỗi khi xóa file {file_path}: {e}")
        
        # Xóa thư mục của bài đăng nếu trống
        if 'stories' in url or '/s/' in url:
            post_dir = os.path.join(DOWNLOAD_DIR, f"stories_{first_part}")
        else:
            post_dir = os.path.join(DOWNLOAD_DIR, first_part)
            
        try:
            if os.path.exists(post_dir) and not os.listdir(post_dir):
                os.rmdir(post_dir)
                logger.info(f"Đã xóa thư mục rỗng: {post_dir}")
        except Exception as e:
            logger.error(f"Lỗi khi xóa thư mục {post_dir}: {e}")
        
        if success_videos > 0 or success_images > 0:
            status_message = []
            if success_videos > 0:
                status_message.append(f"👉 {success_videos} video")
            if success_images > 0:
                status_message.append(f"👉 {success_images} hình ảnh")
            
            # Thêm username vào thông báo thành công
            if 'stories' in url or '/s/' in url:
                await processing_message.edit_text(f"✅ Tải xuống story của @{username} thành công!\n\n" + "\n".join(status_message))
            else:
                # Phân biệt giữa post và reel
                content_type = "reel 📱" if "reel" in url else "post 📑"
                await processing_message.edit_text(f"✅ Tải xuống {content_type} của @{username} thành công!\n\n" + "\n".join(status_message))
        else:
            await processing_message.edit_text("❌ Không thể tải lên nội dung")
    
    except Exception as e:
        logger.error(f"Lỗi xử lý URL Instagram: {e}")
        await processing_message.edit_text(f"❌ Đã xảy ra lỗi: {str(e)}\nVui lòng thử lại sau.")

async def start(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        "👋 Chào mừng đến với Bot Tải Video & Ảnh Instagram!\n\n"
        "Gửi cho tôi URL bài đăng, video ngắn Instagram, và tôi sẽ tải xuống cho bạn.\n\n" 
        "Ví dụ: https://www.instagram.com/p/XXXX/\n\n" 
        "Lưu ý: Bot hỗ trợ tải các định dạng sau: Post, Reel, Story\n\n"
    )

async def help_command(update: Update, context: CallbackContext, return_text: bool = False) -> None:
    """Send a message when the command /help is issued."""
    text = ("📖 *Cách sử dụng bot này:*\n\n"
            "1. Sao chép URL Instagram\n"
            "2. Gửi URL đến bot này\n"
            "3. Đợi bot xử lý và tải xuống\n\n"
            "_Lưu ý: Stories chỉ tồn tại trong 24 giờ và có thể yêu cầu theo dõi tài khoản._\n\n"
            "Nếu bạn gặp bất kỳ vấn đề nào, vui lòng thử lại sau.")
    
    if return_text:
        return text
        
    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Menu chính", callback_data="back_to_menu")
        ]])
    )

async def set_bot_commands(application: Application) -> None:
    """Set bot commands in menu"""
    commands = [
        ("start", "Khởi động bot"),
        ("help", "Xem hướng dẫn sử dụng"),
        ("menu", "Hiển thị menu chức năng")    
    ]
    await application.bot.set_my_commands(commands)

async def menu(update: Update, context: CallbackContext) -> None:
    """Show menu with inline keyboard buttons"""
    keyboard = [
        [
            InlineKeyboardButton("📥 Hướng dẫn tải", callback_data="guide"),
            InlineKeyboardButton("ℹ️ Về bot", callback_data="about")
        ],
        [
            InlineKeyboardButton("🔗 Hỗ trợ định dạng", callback_data="formats"),
            InlineKeyboardButton("❓ Trợ giúp", callback_data="help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🔍 Chọn chức năng bạn muốn sử dụng:",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: CallbackContext) -> None:
    """Handle button callbacks"""
    query = update.callback_query
    await query.answer()  # Acknowledge the button press

    if query.data == "guide":
        text = ("📥 *Hướng dẫn tải xuống:*\n\n"
                "1. Sao chép link Instagram\n"
                "2. Dán trực tiếp vào chat với bot\n"
                "3. Đợi bot xử lý và tải xuống\n\n"
                "_Lưu ý: Bot hỗ trợ tải các định dạng sau:_\n"
                "• Bài đăng (Post)\n"
                "• Video ngắn (Reel)\n"
                "• Story (Stories)")
        
    elif query.data == "formats":
        text = ("🔗 *Các định dạng hỗ trợ:*\n\n"
                "• Bài đăng (Post)\n"
                "• Video ngắn (Reel)\n"
                "• Story (Stories)\n\n"
                "_Có thể dùng link rút gọn hoặc đầy đủ_")
        
    elif query.data == "about":
        text = ("ℹ️ *Thông tin về Bot*\n\n"
                "• Tên: InstaGrap Bot\n"
                "• Chức năng: Tải video, ảnh từ Instagram\n"
                "• Hỗ trợ: Post, Reel, Story\n"
                "• Phiên bản: 1.0\n\n"
                "_Bot được phát triển bởi @sytinhboy_")
        
    elif query.data == "help":
        text = await help_command(update, context, return_text=True)
        
    elif query.data == "back_to_menu":
        # Show menu when back button is clicked
        keyboard = [
            [
                InlineKeyboardButton("📥 Hướng dẫn tải", callback_data="guide"),
                InlineKeyboardButton("ℹ️ Về bot", callback_data="about")
            ],
            [
                InlineKeyboardButton("🔗 Hỗ trợ định dạng", callback_data="formats"),
                InlineKeyboardButton("❓ Trợ giúp", callback_data="help")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="🔍 Chọn chức năng bạn muốn sử dụng:",
            reply_markup=reply_markup
        )
        return
    
    # Only add back button for non-menu screens
    await query.edit_message_text(
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Quay lại Menu", callback_data="back_to_menu")
        ]])
    )

async def about_command(update: Update, context: CallbackContext) -> None:
    """Send information about the bot when the command /about is issued."""
    text = ("ℹ️ *Thông tin về Bot*\n\n"
            "• Tên: InstaGrap Bot\n"
            "• Chức năng: Tải video, ảnh từ Instagram\n"
            "• Hỗ trợ: Post, Reel, Story\n"
            "• Phiên bản: 1.0\n\n"
            "_Bot được phát triển với mục đích phi lợi nhuận_")
    
    await update.message.reply_text(
        text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 Menu chính", callback_data="back_to_menu")
        ]])
    )

async def main() -> None:
    """Start the bot."""
    # Initialize Instagram client
    if not init_instagram_client():
        logger.error("Failed to initialize Instagram client")
        return
    
    # Create the Application
    application = Application.builder().token(Config.TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_instagram_url))
    application.add_handler(CallbackQueryHandler(button_callback))

    # Set bot commands
    await set_bot_commands(application)
    await application.run_polling()

    # Start the bot
    logger.info("Starting bot...")
    
import nest_asyncio
nest_asyncio.apply()

if __name__ == "__main__":
    asyncio.run(main())
