import os
import asyncio
import json
import logging
import aiohttp
import sys

logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)
logger = logging.getLogger()

def log(msg):
    logger.info(msg)

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
OFFSET_FILE = os.getenv("OFFSET_FILE", "/root/crossposting/last_offset.json")


def load_offset() -> int:
    try:
        with open(OFFSET_FILE, "r") as f:
            return json.load(f).get("offset", 0)
    except:
        return 0


def save_offset(offset: int):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


async def resolve_file_id(file_id: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/getFile", params={"file_id": file_id}) as r:
            file_path = (await r.json())["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        async with session.get(file_url) as response:
            return await response.read()


async def send_media_group(chat_id: str, media: list, caption: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/sendMediaGroup", json={
            "chat_id": chat_id,
            "media": [{"type": "photo", "media": m, "caption": caption if i == 0 else ""} 
                      for i, m in enumerate(media)]
        }) as r:
            return await r.json()


async def send_message(chat_id: str, text: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }) as r:
            return await r.json()


NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "-1003753294355")
CHANNEL_CHAT_ID = os.getenv("TELEGRAM_CHANNEL_CHAT_ID")


async def notify_admin(message: str) -> None:
    """Отправка уведомления админу о результатах кросспостинга"""
    log(f"[NOTIFY] Отправка уведомления в {NOTIFY_CHAT_ID}")
    try:
        result = await send_message(NOTIFY_CHAT_ID, message)
        log(f"[NOTIFY] Результат: {result}")
    except Exception as e:
        log(f"[NOTIFY ERROR] {e}")


async def get_updates(offset: int = 0, timeout: int = 60) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/getUpdates", params={"offset": offset, "timeout": timeout}) as r:
            return await r.json()


async def set_webhook(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/setWebhook", json={"url": url}) as r:
            return await r.json()


async def delete_webhook() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/deleteWebhook") as r:
            return await r.json()


carousel_buffer = {}
carousel_lock = asyncio.Lock()


async def start_polling(process_update_callback, last_offset: int = None):
    """Бесконечный цикл polling с обработкой обновлений из канала"""
    if last_offset is None:
        last_offset = load_offset()
    offset = last_offset
    
    log(f"Starting polling from offset {offset}")
    await delete_webhook()
    log("Webhook removed, starting polling...")
    
    while True:
        try:
            log(f"[POLLING] Getting updates with offset {offset}...")
            result = await get_updates(offset=offset, timeout=30)
            log(f"[POLLING] Got result: ok={result.get('ok')}, count={len(result.get('result', []))}")
            print(f"Got result: {result.get('ok')}, count: {len(result.get('result', []))}")
            
            if result.get("ok") and result.get("result"):
                for update in result["result"]:
                    offset = update["update_id"] + 1
                    save_offset(offset)
                    
                    if "channel_post" in update:
                        channel_post = update["channel_post"]
                        
                        if CHANNEL_CHAT_ID and str(channel_post.get("chat", {}).get("id")) != CHANNEL_CHAT_ID:
                            continue
                        
                        asyncio.create_task(process_update_callback({"channel_post": channel_post}))
            
        except Exception as e:
            import traceback
            log(f"[POLLING ERROR] {e}")
            traceback.print_exc()
            await asyncio.sleep(2)