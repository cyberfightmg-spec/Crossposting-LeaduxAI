import os
import uuid
import aiohttp
import logging
import sys
from typing import Tuple
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)
logger = logging.getLogger()

def log(msg):
    logger.info(msg)

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
CAROUSELS_DIR = Path(os.getenv("CAROUSELS_DIR", "/tmp/carousels"))
CAROUSELS_DIR.mkdir(parents=True, exist_ok=True)


async def resolve_file_bytes(file_id: str) -> bytes:
    """Скачать файл из Telegram по file_id"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/getFile", params={"file_id": file_id}) as r:
            result = await r.json()
            file_path = result["result"]["file_path"]
        
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        async with session.get(file_url) as response:
            return await response.read()


async def get_file_info(file_id: str) -> dict:
    """Получить информацию о файле"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/getFile", params={"file_id": file_id}) as r:
            return (await r.json())["result"]


async def download_carousel(file_ids: list) -> Tuple[str, list]:
    """
    Скачать массив фото из Telegram в локальную папку.
    Returns: (carousel_id, list_of_local_paths)
    """
    import time as time_module
    start_time = time_module.time()
    
    carousel_id = str(uuid.uuid4())[:8]
    carousel_dir = CAROUSELS_DIR / carousel_id
    carousel_dir.mkdir(parents=True, exist_ok=True)
    
    local_paths = []
    for i, file_id in enumerate(file_ids):
        img_start = time_module.time()
        try:
            file_info = await get_file_info(file_id)
            file_size = file_info.get("file_size", 0)
            
            img_bytes = await resolve_file_bytes(file_id)
            
            file_path = file_info.get("file_path", "")
            ext = file_path.split(".")[-1] if "." in file_path else "jpg"
            
            path = carousel_dir / f"{i}.{ext}"
            with open(path, "wb") as f:
                f.write(img_bytes)
            local_paths.append(str(path))
            
            img_time = time_module.time() - img_start
            log(f"[CAROUSEL] {i+1}/{len(file_ids)}: {len(img_bytes)} bytes, {img_time:.1f}s")
        except Exception as e:
            log(f"[CAROUSEL ERROR] Failed to download image {i}: {e}")
    
    total_time = time_module.time() - start_time
    log(f"[CAROUSEL] Total download: {len(local_paths)} images in {total_time:.1f}s")
    return carousel_id, local_paths


async def get_uploaded_urls(carousel_id: str, local_paths: list) -> dict:
    """
    Получить публичные URL для скачанных изображений.
    Для продакшена здесь можно загружать в S3/Cloudflare R2/GitHub и т.д.
    Returns: {0: "url1", 1: "url2", ...}
    """
    urls = {}
    for i, path in enumerate(local_paths):
        urls[i] = f"file://{path}"
    return urls


def format_vk_carousel(photo_data: list) -> str:
    """Форматирование для VK carousel/post"""
    return [f'photo{p["owner_id"]}_{p["id"]}' for p in photo_data]


def format_pinterest_links(urls: dict) -> list:
    """Форматирование ссылок для Pinterest"""
    return [urls[i] for i in sorted(urls.keys())]


def format_dzen_links(urls: dict) -> list:
    """Форматирование ссылок для Дзен"""
    return [urls[i] for i in sorted(urls.keys())]


async def cleanup_carousel(carousel_id: str):
    """Удаление временных файлов карусели"""
    import shutil
    carousel_dir = CAROUSELS_DIR / carousel_id
    if carousel_dir.exists():
        shutil.rmtree(carousel_dir)
        log(f"[CAROUSEL] Cleaned up {carousel_dir}")


async def process_carousel(file_ids: list) -> dict:
    """
    Главная функция: скачать карусель и подготовить ссылки для всех платформ.
    Returns: {
        "carousel_id": str,
        "local_paths": list,
        "vk_photos": list,  # [{owner_id, id}, ...]
        "urls": {0: "url", ...},
        "pinterest": list,
        "dzen": list
    }
    """
    carousel_id, local_paths = await download_carousel(file_ids)
    urls = await get_uploaded_urls(carousel_id, local_paths)
    
    return {
        "carousel_id": carousel_id,
        "local_paths": local_paths,
        "urls": urls,
        "pinterest": format_pinterest_links(urls),
        "dzen": format_dzen_links(urls)
    }
