"""Интеграция с Yandex SpeechKit."""
import logging
import requests

from config import settings

log = logging.getLogger("zhorik.speech")


def recognize_telegram_voice(audio: bytes) -> str:
    if not settings.yandex_api_key:
        raise RuntimeError("Для голоса задайте YANDEX_API_KEY в Amvera.")
    params = {"lang": "ru-RU", "format": "oggopus"}
    if settings.yandex_folder_id:
        params["folderId"] = settings.yandex_folder_id
    response = requests.post(
        "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
        params=params,
        headers={"Authorization": f"Api-Key {settings.yandex_api_key}"},
        data=audio,
        timeout=45,
    )
    if response.status_code != 200:
        log.warning("SpeechKit вернул HTTP %s", response.status_code)
        raise RuntimeError("Яндекс не смог распознать голосовое сообщение. Попробуйте ещё раз.")
    text = response.json().get("result", "").strip()
    if not text:
        raise RuntimeError("Не удалось разобрать речь. Попробуйте записать сообщение ещё раз.")
    return text
