import httpx
import os
from dotenv import load_dotenv
load_dotenv()

LINKEDIN_TOKEN = os.getenv("LINKEDIN_TOKEN")
LINKEDIN_AUTHOR = os.getenv("LINKEDIN_AUTHOR_URN")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


def is_configured() -> bool:
    return bool(LINKEDIN_TOKEN and LINKEDIN_AUTHOR)


async def adapt_linkedin(text: str) -> str:
    """AI адаптирует текст для LinkedIn на английском"""
    prompt = f"""You are a LinkedIn content expert. Rewrite this post for LinkedIn in English.

RULES:
- Professional but engaging tone
- Max 1300 characters
- First 2 lines must be a strong hook
- 3-5 relevant hashtags at the end
- No markdown or HTML tags
- Keep the core message and facts

ORIGINAL POST:
{text}

Output ONLY the adapted post, no explanations."""
    
    if GEMINI_API_KEY:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.8}},
                    timeout=30
                )
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"[linkedin] Gemini error: {e}")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={"model": "gpt-4.1-mini", "messages": [{"role": "system", "content": "Rewrite for LinkedIn in English."}, {"role": "user", "content": text}], "temperature": 0.8},
            timeout=30
        )
        return r.json()["choices"][0]["message"]["content"]


async def upload_image_linkedin(image_bytes: bytes) -> str:
    """Загружает изображение и возвращает asset URN"""
    async with httpx.AsyncClient() as client:
        register = await client.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Content-Type": "application/json", "X-Restli-Protocol-Version": "2.0.0"},
            json={"registerUploadRequest": {"owner": LINKEDIN_AUTHOR, "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"], "serviceRelationships": [{"identifier": "urn:li:userGeneratedContent", "relationshipType": "OWNER"}]}},
            timeout=15
        )
        data = register.json()
        upload_url = data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
        asset_urn = data["value"]["asset"]
        await client.put(upload_url, headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}"}, content=image_bytes, timeout=30)
        return asset_urn


async def _create_post(adapted: str, media_category: str = "NONE", media: list = None) -> dict:
    """Внутренняя функция для создания поста LinkedIn"""
    payload = {
        "author": LINKEDIN_AUTHOR,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": adapted},
                "shareMediaCategory": media_category
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
    }
    if media:
        payload["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = media

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.linkedin.com/v2/ugcPosts",
            headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Content-Type": "application/json", "X-Restli-Protocol-Version": "2.0.0"},
            json=payload,
            timeout=20
        )
        return r.json()


async def post_text_linkedin(text: str) -> dict:
    """Текстовый пост в LinkedIn"""
    adapted = await adapt_linkedin(text)
    return await _create_post(adapted, "NONE")


async def post_photo_linkedin(image_bytes: bytes, text: str) -> dict:
    """Пост с одним фото"""
    adapted = await adapt_linkedin(text)
    asset_urn = await upload_image_linkedin(image_bytes)
    return await _create_post(adapted, "IMAGE", [{"status": "READY", "media": asset_urn}])


async def post_carousel_linkedin(images_bytes: list, text: str) -> dict:
    """Пост с несколькими фото (LinkedIn поддерживает множественные вложения)"""
    adapted = await adapt_linkedin(text)
    media = []
    for img_bytes in images_bytes:
        asset_urn = await upload_image_linkedin(img_bytes)
        media.append({"status": "READY", "media": asset_urn})
    return await _create_post(adapted, "IMAGE", media)


async def post_video_linkedin(video_bytes: bytes, text: str) -> dict:
    """Пост с видео"""
    adapted = await adapt_linkedin(text)
    async with httpx.AsyncClient() as client:
        register = await client.post(
            "https://api.linkedin.com/v2/assets?action=registerUpload",
            headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Content-Type": "application/json", "X-Restli-Protocol-Version": "2.0.0"},
            json={"registerUploadRequest": {"owner": LINKEDIN_AUTHOR, "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"], "serviceRelationships": [{"identifier": "urn:li:userGeneratedContent", "relationshipType": "OWNER"}]}},
            timeout=15
        )
        data = register.json()
        upload_url = data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
        asset_urn = data["value"]["asset"]
        await client.put(upload_url, headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Content-Type": "application/octet-stream"}, content=video_bytes, timeout=120)
        return await _create_post(adapted, "VIDEO", [{"status": "READY", "media": asset_urn}])
