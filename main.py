import os
import asyncio
from dotenv import load_dotenv
load_dotenv()

from tools.telegram import notify_admin
from tools.vk import post_text_vk, post_photo_vk, post_video_vk, post_story_vk
from tools.ai_adapter import adapt_vk
from tools.carousel import process_carousel, cleanup_carousel
from tools.max_publisher import (
    is_configured as max_configured,
    post_text as max_post_text,
    post_photo as max_post_photo,
    post_photos as max_post_photos,
    post_video as max_post_video,
)
from tools.linkedin import (
    post_text_linkedin,
    post_photo_linkedin,
    post_carousel_linkedin,
    post_video_linkedin,
    is_configured as linkedin_configured,
)


ENABLED_PLATFORMS = {
    "vk": os.getenv("VK_ENABLED", "true").lower() == "true",
    "linkedin": os.getenv("LINKEDIN_ENABLED", "false").lower() == "true",
    "max": os.getenv("MAX_ENABLED", "false").lower() == "true",
}


def detect_content_type(post: dict) -> str:
    if post.get("_merged"):
        photos = post.get("photo", [])
        if len(photos) > 1:
            return "SLIDES"
        elif len(photos) == 1:
            return "PHOTO"
    if post.get("video") or post.get("animation"):
        return "VIDEO"
    if post.get("text") and not post.get("photo"):
        return "TEXT"
    if post.get("media_group_id"):
        return "SLIDES"
    if post.get("photo"):
        return "PHOTO"
    if post.get("text"):
        return "TEXT"
    return "UNKNOWN"


async def send_crosspost_notification(channel_post: dict, result: dict) -> None:
    content_type = detect_content_type(channel_post)
    preview = ""
    if content_type == "TEXT":
        text = channel_post.get("text", "")[:100]
        preview = f"Текст: {text}..."
    elif content_type == "SLIDES":
        photos = channel_post.get("photo", [])
        count = channel_post.get("_parts_count", len(photos)) if channel_post.get("_merged") else len(photos)
        preview = f"Слайды: {count} фото"
    elif content_type == "PHOTO":
        preview = "Фото"
    elif content_type == "VIDEO":
        preview = "Видео (Reels)"

    chat = channel_post.get("chat", {})
    channel_title = chat.get("title", "Неизвестный канал")
    channel_username = chat.get("username", "")
    
    if channel_username:
        channel_name = f"@{channel_username}"
    else:
        channel_name = channel_title

    from datetime import datetime
    
    publish_time = datetime.now().strftime("%d.%m.%Y %H:%M")
    
    msg = f"📤 Кросспостинг\n\nКанал: {channel_name}\n{preview}\n\n"
    
    platforms = result.get("platforms", {})
    platform_names = {
        "vk": "VK",
        "vk_story": "VK Stories",
        "linkedin": "LinkedIn",
        "max": "Max",
    }
    
    # Собираем платформы в группы
    active_platforms = []  # Включенные и обработанные
    disabled_platforms = []  # Отключенные в .env
    
    for platform, enabled in ENABLED_PLATFORMS.items():
        name = platform_names.get(platform, platform.capitalize())
        status = platforms.get(platform, "disabled" if not enabled else "not_processed")
        
        if not enabled:
            disabled_platforms.append((name, "disabled"))
        elif status == "ok":
            active_platforms.append((name, "ok", publish_time))
        elif status == "error":
            active_platforms.append((name, "error", None))
        elif status == "logged":
            active_platforms.append((name, "logged", None))
        elif status == "disabled":
            disabled_platforms.append((name, "disabled"))
    
    # Сначала выводим активные
    for name, status, time in active_platforms:
        if status == "ok":
            msg += f"✅ {name:<12} ({time})\n"
        elif status == "error":
            msg += f"❌ {name:<12} (ошибка)\n"
        elif status == "logged":
            msg += f"📝 {name:<12} (залогировано)\n"
    
    # Затем отключенные
    for name, status in disabled_platforms:
        msg += f"⏸ {name:<12} (отключено)\n"
    
    if result.get("errors"):
        msg += f"\nОшибки: {result['errors']}"
    
    await notify_admin(msg)




carousel_cache = {}
carousel_lock = asyncio.Lock()
carousel_tasks = {}


async def _fire_carousel(media_group_id: str):
    """Фоновая задача: ждёт 5 секунд после первого фото, затем мёрджит и постит карусель"""
    await asyncio.sleep(5.0)

    async with carousel_lock:
        if media_group_id not in carousel_cache:
            return
        entry = carousel_cache.pop(media_group_id)
        carousel_tasks.pop(media_group_id, None)

    parts = entry["parts"]
    print(f"[CAROUSEL] Firing {media_group_id}: {len(parts)} parts collected")

    try:
        merged = merge_parts(parts)
        await _do_crosspost(merged)
    except Exception as e:
        import traceback
        print(f"[CAROUSEL ERROR] {media_group_id}: {e}")
        traceback.print_exc()


def merge_parts(parts: list) -> dict:
    """Объединяет части карусели в один пост - только оригиналы (макс. размер)"""
    all_photos = []
    caption = ""
    
    print(f"[MERGE] Received {len(parts)} parts")
    for i, part in enumerate(parts):
        photos = part.get("photo", [])
        print(f"[MERGE] Part {i}: {len(photos)} photos")
        
        # Debug: print all file_ids to understand structure
        seen_file_ids = set()
        for j, p in enumerate(photos):
            file_id = p.get("file_id", "n/a")
            file_size = p.get("file_size", 0)
            width = p.get("width", 0)
            is_new = file_id not in seen_file_ids
            if is_new:
                seen_file_ids.add(file_id)
            print(f"[MERGE]   Photo {j}: id={file_id[:30]}... w={width} size={file_size} is_new={is_new}")
        
        # Take only the last photo (max resolution)
        if photos:
            original = photos[-1]
            file_id = original.get("file_id", "no_id")
            file_size = original.get("file_size", 0)
            print(f"[MERGE] Taking original: id={file_id[:20]}... size={file_size}")
            all_photos.append(original)
        
        if not caption:
            caption = part.get("caption") or part.get("text", "")
    
    merged = parts[0].copy()
    merged["photo"] = all_photos
    merged["caption"] = caption
    merged["_merged"] = True
    merged["_parts_count"] = len(parts)
    print(f"[MERGE] Total photos: {len(all_photos)}")
    return merged


async def crosspost(update: dict) -> dict:
    """Router: буферизует карусели, остальной контент публикует сразу"""
    channel_post = update.get("channel_post", {})
    media_group_id = channel_post.get("media_group_id")

    if media_group_id:
        async with carousel_lock:
            if media_group_id not in carousel_cache:
                carousel_cache[media_group_id] = {"parts": []}
                task = asyncio.create_task(_fire_carousel(media_group_id))
                carousel_tasks[media_group_id] = task
                print(f"[CAROUSEL] Started buffering {media_group_id}")
            carousel_cache[media_group_id]["parts"].append(channel_post)
            count = len(carousel_cache[media_group_id]["parts"])
            print(f"[CAROUSEL] Buffered part {count} for {media_group_id}")
        return {"status": "buffering"}

    return await _do_crosspost(channel_post)


async def _do_crosspost(channel_post: dict) -> dict:
    result = {"status": "ok", "platforms": {}, "errors": []}

    content_type = detect_content_type(channel_post)
    photos = channel_post.get("photo", [])
    print(f"[DEBUG] content_type={content_type}, photos={len(photos)}, merged={channel_post.get('_merged', False)}")

    if content_type == "TEXT":
        text = channel_post.get("text", "")

        async def run_vk():
            if not ENABLED_PLATFORMS["vk"]:
                result["platforms"]["vk"] = "disabled"
                return
            try:
                adapted = await adapt_vk(text)
                vk_result = await post_text_vk(adapted)
                result["platforms"]["vk"] = "ok" if vk_result.get("response") else "error"
            except Exception as e:
                result["platforms"]["vk"] = "error"
                result["errors"].append(f"vk: {str(e)}")

        async def run_max():
            if not ENABLED_PLATFORMS["max"]:
                result["platforms"]["max"] = "disabled"
                return
            if not max_configured():
                result["platforms"]["max"] = "error"
                result["errors"].append("max: not configured")
                return
            try:
                entities = channel_post.get("entities", [])
                max_result = await max_post_text(text, entities)
                result["platforms"]["max"] = "ok" if not max_result.get("error") else "error"
                if max_result.get("error"):
                    result["errors"].append(f"max: {max_result.get('error')}")
            except Exception as e:
                result["platforms"]["max"] = "error"
                result["errors"].append(f"max: {str(e)}")

        async def run_linkedin():
            if not ENABLED_PLATFORMS.get("linkedin", False):
                result["platforms"]["linkedin"] = "disabled"
                return
            try:
                li_result = await post_text_linkedin(text)
                result["platforms"]["linkedin"] = "ok" if not li_result.get("error") else "error"
                if li_result.get("error"):
                    result["errors"].append(f"linkedin: {li_result.get('error')}")
            except Exception as e:
                result["platforms"]["linkedin"] = "error"
                result["errors"].append(f"linkedin: {str(e)}")

        await asyncio.gather(run_vk(), run_max(), run_linkedin())

    elif content_type == "SLIDES":
        file_ids = [p["file_id"] for p in photos]
        caption = channel_post.get("caption", "")
        adapted_caption_vk = await adapt_vk(caption) if caption else ""
        print(f"[DEBUG] SLIDES: {len(file_ids)} photos, {len(set(file_ids))} unique file_ids")

        carousel = await process_carousel(file_ids)

        async def run_vk():
            if not ENABLED_PLATFORMS["vk"]:
                result["platforms"]["vk"] = "disabled"
                return
            try:
                vk_result = await post_photo_vk(carousel["local_paths"], adapted_caption_vk or caption, carousel=True)
                if vk_result.get("response"):
                    result["platforms"]["vk"] = "ok"
                else:
                    result["platforms"]["vk"] = "error"
                    result["errors"].append(f"vk: {vk_result.get('error', 'unknown')}")
            except Exception as e:
                result["platforms"]["vk"] = "error"
                result["errors"].append(f"vk: {str(e)}")

        async def run_vk_story():
            if not ENABLED_PLATFORMS["vk"]:
                result["platforms"]["vk_story"] = "disabled"
                return
            try:
                photo_bytes = open(carousel["local_paths"][0], "rb").read()
                story_result = await post_story_vk(photo_bytes, adapted_caption_vk or caption)
                result["platforms"]["vk_story"] = "ok" if story_result.get("response", {}).get("count", 0) > 0 else "error"
                if story_result.get("response", {}).get("count", 0) == 0:
                    result["errors"].append(f"vk_story: {story_result.get('error', 'no stories saved')}")
            except Exception as e:
                result["platforms"]["vk_story"] = "error"
                result["errors"].append(f"vk_story: {str(e)}")

        await asyncio.gather(run_vk(), run_vk_story())
        
        async def run_max_slides():
            if not ENABLED_PLATFORMS["max"]:
                result["platforms"]["max"] = "disabled"
                return
            if not max_configured():
                result["platforms"]["max"] = "error"
                result["errors"].append("max: not configured")
                return
            try:
                photos_bytes = []
                for path in carousel["local_paths"][:10]:
                    with open(path, "rb") as f:
                        photos_bytes.append(f.read())
                
                caption_entities = channel_post.get("caption_entities", [])
                max_results = await max_post_photos(photos_bytes, caption, caption_entities)
                
                all_ok = all(not r.get("error") for r in max_results)
                result["platforms"]["max"] = "ok" if all_ok else "error"
                if not all_ok:
                    errors = [r.get("error") for r in max_results if r.get("error")]
                    result["errors"].append(f"max: {errors}")
            except Exception as e:
                result["platforms"]["max"] = "error"
                result["errors"].append(f"max: {str(e)}")
        
        async def run_linkedin_slides():
            if not ENABLED_PLATFORMS.get("linkedin", False):
                result["platforms"]["linkedin"] = "disabled"
                return
            if not linkedin_configured():
                result["platforms"]["linkedin"] = "error"
                result["errors"].append("linkedin: not configured")
                return
            try:
                photo_bytes_list = []
                for path in carousel["local_paths"]:
                    with open(path, "rb") as f:
                        photo_bytes_list.append(f.read())
                li_result = await post_carousel_linkedin(photo_bytes_list, caption)
                result["platforms"]["linkedin"] = "ok" if not li_result.get("error") else "error"
                if li_result.get("error"):
                    result["errors"].append(f"linkedin: {li_result.get('error')}")
            except Exception as e:
                result["platforms"]["linkedin"] = "error"
                result["errors"].append(f"linkedin: {str(e)}")

        await run_max_slides()
        await run_linkedin_slides()
        await cleanup_carousel(carousel["carousel_id"])

    elif content_type == "PHOTO":
        file_id = channel_post["photo"][-1]["file_id"]
        caption = channel_post.get("caption", "")
        print(f"[ADAPT] Original caption: {caption[:100]}...")
        adapted_caption_vk = await adapt_vk(caption) if caption else ""
        print(f"[ADAPT] Adapted caption: {adapted_caption_vk[:100]}...")

        carousel = await process_carousel([file_id])

        async def run_vk():
            if not ENABLED_PLATFORMS["vk"]:
                result["platforms"]["vk"] = "disabled"
                return
            try:
                vk_result = await post_photo_vk(carousel["local_paths"], adapted_caption_vk or caption)
                result["platforms"]["vk"] = "ok" if vk_result.get("response") else "error"
            except Exception as e:
                result["platforms"]["vk"] = "error"
                result["errors"].append(f"vk: {str(e)}")

        async def run_vk_story():
            if not ENABLED_PLATFORMS["vk"]:
                result["platforms"]["vk_story"] = "disabled"
                return
            try:
                photo_bytes = open(carousel["local_paths"][0], "rb").read()
                story_result = await post_story_vk(photo_bytes, adapted_caption_vk or caption)
                result["platforms"]["vk_story"] = "ok" if story_result.get("response", {}).get("count", 0) > 0 else "error"
                if story_result.get("response", {}).get("count", 0) == 0:
                    result["errors"].append(f"vk_story: {story_result.get('error', 'no stories saved')}")
            except Exception as e:
                result["platforms"]["vk_story"] = "error"
                result["errors"].append(f"vk_story: {str(e)}")

        await asyncio.gather(run_vk(), run_vk_story())
        
        async def run_max_photo():
            print("[MAX] Starting max photo upload...")
            if not ENABLED_PLATFORMS["max"]:
                print("[MAX] Disabled in settings")
                result["platforms"]["max"] = "disabled"
                return
            if not max_configured():
                print("[MAX] Not configured")
                result["platforms"]["max"] = "error"
                result["errors"].append("max: not configured")
                return
            try:
                with open(carousel["local_paths"][0], "rb") as f:
                    photo_bytes = f.read()
                print(f"[MAX] Read photo: {len(photo_bytes)} bytes")
                
                caption_entities = channel_post.get("caption_entities", [])
                max_result = await max_post_photo(photo_bytes, caption, caption_entities)
                print(f"[MAX] Result: {max_result}")
                result["platforms"]["max"] = "ok" if not max_result.get("error") else "error"
                if max_result.get("error"):
                    result["errors"].append(f"max: {max_result.get('error')}")
            except Exception as e:
                import traceback
                print(f"[MAX] Error: {e}")
                traceback.print_exc()
                result["platforms"]["max"] = "error"
                result["errors"].append(f"max: {str(e)}")
        
        async def run_linkedin_photo():
            if not ENABLED_PLATFORMS.get("linkedin", False):
                result["platforms"]["linkedin"] = "disabled"
                return
            if not linkedin_configured():
                result["platforms"]["linkedin"] = "error"
                result["errors"].append("linkedin: not configured")
                return
            try:
                with open(carousel["local_paths"][0], "rb") as f:
                    photo_bytes = f.read()
                li_result = await post_photo_linkedin(photo_bytes, caption)
                result["platforms"]["linkedin"] = "ok" if not li_result.get("error") else "error"
                if li_result.get("error"):
                    result["errors"].append(f"linkedin: {li_result.get('error')}")
            except Exception as e:
                result["platforms"]["linkedin"] = "error"
                result["errors"].append(f"linkedin: {str(e)}")

        await run_max_photo()
        await run_linkedin_photo()
        await cleanup_carousel(carousel["carousel_id"])

    elif content_type == "VIDEO":
        video_obj = channel_post.get("video") or channel_post.get("animation", {})
        caption   = channel_post.get("caption", "")
        print(f"[ADAPT] Original caption: {caption[:100]}...")
        adapted_caption_vk = await adapt_vk(caption) if caption else ""
        print(f"[ADAPT] Adapted caption: {adapted_caption_vk[:100]}...")
        file_id   = video_obj.get("file_id", "")
        file_size = video_obj.get("file_size", 0)
        thumb_obj = video_obj.get("thumbnail") or video_obj.get("thumb")

        TG_MAX_BYTES = 20 * 1024 * 1024
        if file_size and file_size > TG_MAX_BYTES:
            msg = f"⚠️ Видео слишком большое ({file_size // 1024 // 1024} МБ > 20 МБ), пропускаем"
            print(f"[VIDEO] {msg}")
            await notify_admin(msg)
            return result

        from tools.telegram import resolve_file_id

        print(f"[VIDEO] Скачиваем видео file_id={file_id[:20]}... size={file_size}")
        video_bytes = await resolve_file_id(file_id)
        print(f"[VIDEO] Загружено {len(video_bytes)} байт")

        thumbnail_bytes = None
        if thumb_obj:
            try:
                thumbnail_bytes = await resolve_file_id(thumb_obj["file_id"])
            except Exception as e:
                print(f"[VIDEO] Не удалось скачать превью: {e}")

        async def run_vk_video():
            if not ENABLED_PLATFORMS["vk"]:
                result["platforms"]["vk"] = "disabled"
                return
            try:
                vk_result = await post_video_vk(video_bytes, adapted_caption_vk or caption)
                result["platforms"]["vk"] = "ok" if vk_result.get("response") else "error"
                if not vk_result.get("response"):
                    result["errors"].append(f"vk: {vk_result.get('error', 'unknown')}")
            except Exception as e:
                result["platforms"]["vk"] = "error"
                result["errors"].append(f"vk: {str(e)}")

        async def run_max_video():
            if not ENABLED_PLATFORMS["max"]:
                result["platforms"]["max"] = "disabled"
                return
            if not max_configured():
                result["platforms"]["max"] = "error"
                result["errors"].append("max: not configured")
                return
            try:
                caption_entities = channel_post.get("caption_entities", [])
                print(f"[MAX VIDEO] Starting upload, video size: {len(video_bytes)} bytes, caption: {(caption or '')[:80]}")
                max_result = await max_post_video(video_bytes, caption, caption_entities)
                print(f"[MAX VIDEO] Result: {max_result}")
                result["platforms"]["max"] = "ok" if not max_result.get("error") else "error"
                if max_result.get("error"):
                    result["errors"].append(f"max: {max_result.get('error')}")
            except Exception as e:
                import traceback
                print(f"[MAX VIDEO] Exception: {e}")
                traceback.print_exc()
                result["platforms"]["max"] = "error"
                result["errors"].append(f"max: {str(e)}")

        async def run_linkedin_video():
            if not ENABLED_PLATFORMS.get("linkedin", False):
                result["platforms"]["linkedin"] = "disabled"
                return
            if not linkedin_configured():
                result["platforms"]["linkedin"] = "error"
                result["errors"].append("linkedin: not configured")
                return
            try:
                li_result = await post_video_linkedin(video_bytes, caption)
                result["platforms"]["linkedin"] = "ok" if not li_result.get("error") else "error"
                if li_result.get("error"):
                    result["errors"].append(f"linkedin: {li_result.get('error')}")
            except Exception as e:
                result["platforms"]["linkedin"] = "error"
                result["errors"].append(f"linkedin: {str(e)}")

        await asyncio.gather(run_vk_video(), run_max_video(), run_linkedin_video())

    if result["errors"]:
        result["status"] = "partial" if result["platforms"] else "error"
    else:
        result["status"] = "ok"

    await send_crosspost_notification(channel_post, result)
    return result


async def run_polling():
    """Запуск polling для получения обновлений из Telegram канала"""
    from tools.telegram import start_polling, load_offset
    
    offset = load_offset()
    print(f"Starting polling from offset: {offset}")
    
    async def handle_update(update: dict):
        result = await crosspost(update)
        return result
    
    await start_polling(handle_update, offset)


if __name__ == "__main__":
    asyncio.run(run_polling())