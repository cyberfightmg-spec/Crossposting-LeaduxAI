"""
Max Publisher Module
Интеграция с мессенджером Max для кросспостинга
"""
import os
import asyncio
from typing import Optional, List, Dict, Any
from pathlib import Path
import httpx
from dotenv import load_dotenv

load_dotenv()

# Configuration
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_CHANNEL_ID = os.getenv("MAX_CHANNEL_ID")
MAX_ENABLED = os.getenv("MAX_ENABLED", "false").lower() == "true"

# API Constants
MAX_BASE_URL = "https://platform-api.max.ru"
MAX_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Rate limiting
MAX_RPS = 30
MAX_RETRY_DELAY = 2  # seconds for 429
MAX_MAX_RETRIES = 3


def is_configured() -> bool:
    """Проверяет, настроена ли интеграция с Max"""
    return bool(MAX_BOT_TOKEN and MAX_CHANNEL_ID)


def convert_tg_entities_to_markdown(text: str, entities: List[Dict[str, Any]]) -> str:
    """
    Конвертирует Telegram entities в Markdown для Max.
    Обрабатывает entities с конца текста к началу, чтобы смещения не съезжали.
    
    Supported entities:
    - bold → **text**
    - italic → *text*
    - text_link → [text](url)
    - url → url (или [url](url))
    - code → `text`
    - strikethrough → ~~text~~
    """
    if not entities:
        return text
    
    # Сортируем entities по убыванию offset (с конца к началу)
    sorted_entities = sorted(entities, key=lambda e: e.get("offset", 0), reverse=True)
    
    result = text
    for entity in sorted_entities:
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        entity_type = entity.get("type", "")
        
        if offset < 0 or length <= 0 or offset >= len(result):
            continue
        
        end_pos = min(offset + length, len(result))
        entity_text = result[offset:end_pos]
        
        if entity_type == "bold":
            replacement = f"**{entity_text}**"
        elif entity_type == "italic":
            replacement = f"*{entity_text}*"
        elif entity_type == "text_link":
            url = entity.get("url", "")
            replacement = f"[{entity_text}]({url})"
        elif entity_type == "url":
            # Голую ссылку можно оставить как есть или обернуть
            replacement = entity_text
        elif entity_type == "code":
            replacement = f"`{entity_text}`"
        elif entity_type == "strikethrough":
            replacement = f"~~{entity_text}~~"
        else:
            continue
        
        result = result[:offset] + replacement + result[end_pos:]
    
    return result


class MaxPublisher:
    """Класс для публикации контента в мессенджер Max"""
    
    def __init__(self, bot_token: Optional[str] = None, channel_id: Optional[str] = None):
        self.bot_token = bot_token or MAX_BOT_TOKEN
        self.channel_id = channel_id or MAX_CHANNEL_ID
        self.base_url = MAX_BASE_URL
        self.client: Optional[httpx.AsyncClient] = None
        
        if not self.bot_token:
            raise ValueError("MAX_BOT_TOKEN not set")
        if not self.channel_id:
            raise ValueError("MAX_CHANNEL_ID not set")
        
        # Убедимся что channel_id - число
        try:
            self.channel_id_int = int(self.channel_id)
        except ValueError:
            raise ValueError(f"MAX_CHANNEL_ID must be a number, got: {self.channel_id}")
    
    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            headers={
                "Authorization": self.bot_token,
                "Content-Type": "application/json"
            },
            timeout=60.0
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.client:
            await self.client.aclose()
    
    def _make_url(self, endpoint: str, **params) -> str:
        """Создает URL с query параметрами"""
        url = f"{self.base_url}{endpoint}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{query}"
        return url
    
    async def _request_with_retry(
        self, 
        method: str, 
        endpoint: str, 
        json_data: Optional[Dict] = None,
        files: Optional[Dict] = None,
        **params
    ) -> Dict[str, Any]:
        """
        Выполняет HTTP-запрос с retry-логикой.
        
        - 401: сразу raise Exception
        - 429: подождать 2 сек, retry 1 раз
        - 5xx: retry до 3 раз с экспоненциальной задержкой (1с, 2с, 4с)
        """
        if not self.client:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        
        url = self._make_url(endpoint, **params)
        
        retries = 0
        max_retries = MAX_MAX_RETRIES
        
        while True:
            try:
                if method.upper() == "GET":
                    response = await self.client.get(url)
                elif method.upper() == "POST":
                    if files:
                        # Для multipart загрузки убираем Content-Type из headers
                        headers = dict(self.client.headers)
                        headers.pop("Content-Type", None)
                        response = await self.client.post(
                            url, 
                            files=files,
                            headers=headers
                        )
                    else:
                        response = await self.client.post(url, json=json_data)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                
                # Обработка ошибок
                if response.status_code == 401:
                    raise Exception(f"Max API authentication failed (401). Check your MAX_BOT_TOKEN.")
                
                if response.status_code == 429:
                    if retries < 1:  # Только 1 retry для 429
                        await asyncio.sleep(MAX_RETRY_DELAY)
                        retries += 1
                        continue
                    else:
                        response.raise_for_status()
                
                if response.status_code >= 500:
                    if retries < max_retries:
                        delay = 2 ** retries  # 1с, 2с, 4с
                        await asyncio.sleep(delay)
                        retries += 1
                        continue
                    else:
                        response.raise_for_status()
                
                response.raise_for_status()
                return response.json()
                
            except httpx.HTTPStatusError as e:
                if retries >= max_retries:
                    raise
                retries += 1
                await asyncio.sleep(2 ** (retries - 1))
    
    async def upload_media(self, file_bytes: bytes, file_type: str) -> Dict[str, str]:
        """
        Загрузка медиа в Max (двухэтапный процесс).
        
        Для video/audio: токен приходит из /uploads, а не из ответа загрузки файла.
        """
        # Шаг 1: Получаем URL для загрузки
        print(f"[MAX UPLOAD] Requesting upload URL for type={file_type}, size={len(file_bytes)} bytes")
        upload_info = await self._request_with_retry(
            "POST",
            "/uploads",
            type=file_type
        )
        print(f"[MAX UPLOAD] Upload info response: {upload_info}")
        
        upload_url = upload_info.get("url")
        if not upload_url:
            raise Exception(f"Invalid upload response: {upload_info}")
        
        # Для video/audio токен приходит сразу из /uploads
        pre_upload_token = upload_info.get("token")
        
        # Шаг 2: Загружаем файл по полученному URL
        filename = "video.mp4" if file_type == "video" else "file"
        files = {
            "data": (filename, file_bytes)
        }
        
        # Для загрузки используем отдельный клиент без auth header
        async with httpx.AsyncClient(timeout=300.0) as upload_client:
            upload_response = await upload_client.post(
                upload_url,
                files=files
            )
            print(f"[MAX UPLOAD] Status: {upload_response.status_code}, type: {file_type}")
            if upload_response.status_code != 200:
                print(f"[MAX UPLOAD] Error response: {upload_response.text[:500]}")
                raise Exception(f"Upload failed: {upload_response.status_code}")
        
        # Шаг 3: Извлекаем токен
        # Для video/audio: токен из /uploads ответа
        if file_type in ("video", "audio") and pre_upload_token:
            print(f"[MAX UPLOAD] Using pre-upload token for {file_type}")
            await asyncio.sleep(3)  # Ждём обработки видео
            return {
                "token": pre_upload_token,
                "type": file_type
            }
        
        # Для image/file: парсим ответ загрузки
        try:
            upload_result = upload_response.json()
        except:
            print(f"[MAX UPLOAD] Not JSON response: {upload_response.text[:500]}")
            raise Exception(f"Invalid upload response")
        
        print(f"[MAX UPLOAD] Upload result for {file_type}: {upload_result}")
        upload_token = None
        
        if file_type == "image":
            photos = upload_result.get("photos", {})
            if photos:
                first_photo = list(photos.values())[0]
                upload_token = first_photo.get("token")
        elif file_type == "file":
            upload_token = upload_result.get("token")
            if not upload_token and "files" in upload_result:
                files_data = upload_result.get("files", {})
                if files_data:
                    first_file = list(files_data.values())[0]
                    upload_token = first_file.get("token")
        else:
            # video/audio fallback: токен из ответа загрузки
            upload_token = upload_result.get("token")
        
        if not upload_token:
            raise Exception(f"Failed to get upload token for {file_type}. Response: {upload_result}")
        
        return {
            "token": upload_token,
            "type": file_type
        }
    
    async def send_message(
        self, 
        text: str, 
        attachments: Optional[List[Dict]] = None,
        format_type: str = "markdown"
    ) -> Dict[str, Any]:
        """
        Отправляет сообщение в канал Max с retry для attachment.not.ready.
        
        Args:
            text: Текст сообщения
            attachments: Список вложений (опционально)
            format_type: Форматирование - 'markdown' или 'html'
        
        Returns:
            Ответ API Max
        """
        payload = {
            "text": text,
            "format": format_type
        }
        
        if attachments:
            # Если есть вложения, добавляем их (только token, без URL)
            payload["attachments"] = [
                {
                    "type": att["type"],
                    "payload": {
                        "token": att["token"]
                    }
                }
                for att in attachments
            ]
        
        # Retry для attachment.not.ready (до 5 попыток с увеличивающейся задержкой)
        max_retries = 5
        base_retry_delay = 3.0  # Видео обрабатывается дольше
        
        for attempt in range(max_retries):
            try:
                result = await self._request_with_retry(
                    "POST",
                    f"/messages",
                    json_data=payload,
                    chat_id=self.channel_id_int
                )
                return result
            except httpx.HTTPStatusError as e:
                # Проверяем ошибку attachment.not.ready
                if e.response.status_code == 400:
                    try:
                        error_data = e.response.json()
                        if error_data.get("code") == "attachment.not.ready":
                            retry_delay = base_retry_delay * (2 ** attempt)  # 3, 6, 12, 24
                            print(f"[MAX SEND] Attachment not ready, retry {attempt+1}/{max_retries} in {retry_delay}s")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(retry_delay)
                                continue
                    except:
                        pass
                raise
        
        return {"error": "Max retries exceeded for attachment.not.ready"}
    
    async def send_text(self, text: str, entities: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """
        Отправляет текстовое сообщение с конвертацией entities.
        
        Args:
            text: Текст сообщения
            entities: Telegram entities для конвертации в markdown
        
        Returns:
            Ответ API Max
        """
        if entities:
            markdown_text = convert_tg_entities_to_markdown(text, entities)
        else:
            markdown_text = text
        
        return await self.send_message(markdown_text, format_type="markdown")
    
    async def send_photo(
        self, 
        photo_bytes: bytes, 
        caption: str = "",
        caption_entities: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Отправляет фото с подписью в Max.
        
        Args:
            photo_bytes: Бинарные данные изображения
            caption: Подпись к фото
            caption_entities: Entities подписи для конвертации
        
        Returns:
            Ответ API Max
        """
        # Загружаем фото
        media_info = await self.upload_media(photo_bytes, "image")
        
        # Конвертируем caption в markdown если есть entities
        if caption_entities:
            markdown_caption = convert_tg_entities_to_markdown(caption, caption_entities)
        else:
            markdown_caption = caption
        
        # Отправляем сообщение с вложением
        return await self.send_message(
            text=markdown_caption,
            attachments=[media_info],
            format_type="markdown"
        )
    
    async def send_photos_group(
        self, 
        photos_bytes: List[bytes], 
        caption: str = "",
        caption_entities: Optional[List[Dict]] = None
    ) -> List[Dict[str, Any]]:
        """
        Отправляет группу фото ОДНИМ сообщением в Max.
        
        Args:
            photos_bytes: Список бинарных данных изображений
            caption: Подпись к посту
            caption_entities: Entities подписи
        
        Returns:
            Список с одним ответом API Max (одно сообщение)
        """
        if not photos_bytes:
            return [{"error": "no photos", "status": "error"}]
        
        # Загружаем все фото
        attachments = []
        for photo_bytes in photos_bytes:
            media_info = await self.upload_media(photo_bytes, "image")
            attachments.append(media_info)
        
        # Конвертируем caption в markdown если есть entities
        if caption_entities:
            text = convert_tg_entities_to_markdown(caption, caption_entities)
        else:
            text = caption
        
        # Отправляем ОДНО сообщение со всеми фото
        result = await self.send_message(
            text=text,
            attachments=attachments,
            format_type="markdown"
        )
        
        return [result]
    
    async def send_video(
        self, 
        video_bytes: bytes, 
        caption: str = "",
        caption_entities: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Отправляет видео с подписью в Max.
        
        Args:
            video_bytes: Бинарные данные видео
            caption: Подпись к видео
            caption_entities: Entities подписи для конвертации
        
        Returns:
            Ответ API Max
        """
        # Загружаем видео
        media_info = await self.upload_media(video_bytes, "video")
        
        # Конвертируем caption в markdown если есть entities
        if caption_entities:
            markdown_caption = convert_tg_entities_to_markdown(caption, caption_entities)
        else:
            markdown_caption = caption
        
        # Отправляем сообщение с вложением
        return await self.send_message(
            text=markdown_caption,
            attachments=[media_info],
            format_type="markdown"
        )
    
    async def send_document(
        self, 
        document_bytes: bytes, 
        caption: str = "",
        caption_entities: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Отправляет документ с подписью в Max.
        
        Args:
            document_bytes: Бинарные данные документа
            caption: Подпись к документу
            caption_entities: Entities подписи для конвертации
        
        Returns:
            Ответ API Max
        """
        # Загружаем документ
        media_info = await self.upload_media(document_bytes, "file")
        
        # Конвертируем caption в markdown если есть entities
        if caption_entities:
            markdown_caption = convert_tg_entities_to_markdown(caption, caption_entities)
        else:
            markdown_caption = caption
        
        # Отправляем сообщение с вложением
        return await self.send_message(
            text=markdown_caption,
            attachments=[media_info],
            format_type="markdown"
        )


# Convenience functions for direct use

async def post_text(text: str, entities: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """
    Отправляет текстовое сообщение в Max ( convenience function ).
    
    Args:
        text: Текст сообщения
        entities: Telegram entities для конвертации
    
    Returns:
        Ответ API Max
    """
    if not is_configured():
        return {"error": "Max not configured", "status": "error"}
    
    async with MaxPublisher() as publisher:
        return await publisher.send_text(text, entities)


async def post_photo(
    photo_bytes: bytes, 
    caption: str = "",
    caption_entities: Optional[List[Dict]] = None
) -> Dict[str, Any]:
    """
    Отправляет фото в Max ( convenience function ).
    
    Args:
        photo_bytes: Бинарные данные изображения
        caption: Подпись к фото
        caption_entities: Entities подписи
    
    Returns:
        Ответ API Max
    """
    if not is_configured():
        return {"error": "Max not configured", "status": "error"}
    
    async with MaxPublisher() as publisher:
        return await publisher.send_photo(photo_bytes, caption, caption_entities)


async def post_photos(
    photos_bytes: List[bytes], 
    caption: str = "",
    caption_entities: Optional[List[Dict]] = None
) -> List[Dict[str, Any]]:
    """
    Отправляет группу фото в Max ( convenience function ).
    
    Args:
        photos_bytes: Список бинарных данных изображений
        caption: Подпись (только к первому фото)
        caption_entities: Entities подписи
    
    Returns:
        Список ответов API Max
    """
    if not is_configured():
        return [{"error": "Max not configured", "status": "error"}]
    
    async with MaxPublisher() as publisher:
        return await publisher.send_photos_group(photos_bytes, caption, caption_entities)


def limit_hashtags(text: str, max_tags: int = 3) -> str:
    """Ограничивает количество хэштегов до max_tags"""
    import re
    hashtags = re.findall(r'#[а-яёa-zA-Z0-9_]+', text)
    if len(hashtags) <= max_tags:
        return text
    keep_tags = hashtags[:max_tags]
    text_no_tags = re.sub(r'#[а-яёa-zA-Z0-9_]+', '', text)
    text_no_tags = re.sub(r'\n{3,}', '\n\n', text_no_tags)
    text_no_tags = re.sub(r' {2,}', ' ', text_no_tags)
    tags_str = ' '.join(keep_tags)
    return text_no_tags.strip() + '\n\n' + tags_str


async def post_video(
    video_bytes: bytes, 
    caption: str = "",
    caption_entities: Optional[List[Dict]] = None
) -> Dict[str, Any]:
    """
    Отправляет видео в Max ( convenience function ).
    
    Args:
        video_bytes: Бинарные данные видео
        caption: Подпись к видео
        caption_entities: Entities подписи
    
    Returns:
        Ответ API Max
    """
    if not is_configured():
        return {"error": "Max not configured", "status": "error"}
    
    # Ограничиваем хэштеги до 3 для видео
    caption = limit_hashtags(caption, max_tags=3)
    
    async with MaxPublisher() as publisher:
        return await publisher.send_video(video_bytes, caption, caption_entities)


async def post_document(
    document_bytes: bytes, 
    caption: str = "",
    caption_entities: Optional[List[Dict]] = None
) -> Dict[str, Any]:
    """
    Отправляет документ в Max ( convenience function ).
    
    Args:
        document_bytes: Бинарные данные документа
        caption: Подпись к документу
        caption_entities: Entities подписи
    
    Returns:
        Ответ API Max
    """
    if not is_configured():
        return {"error": "Max not configured", "status": "error"}
    
    async with MaxPublisher() as publisher:
        return await publisher.send_document(document_bytes, caption, caption_entities)
