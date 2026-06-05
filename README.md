# Crosspost Bot

Telegram → VK + LinkedIn + Max

Бот забирает посты из Telegram-канала и публикует в VK (посты + stories), LinkedIn (текст, фото, карусель, видео) и Max.

## Установка

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Настройка

1. Скопируйте `.env.example` в `.env` и заполните токены
2. Запустите: `python3 main.py`

## Требования

- Python 3.10+
- Telegram Bot Token
- VK Group Token (ключ доступа сообщества)
- LinkedIn Access Token
- Max Bot Token
