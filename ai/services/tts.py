"""
Google Cloud Text-to-Speech servisi.

Firebase Cloud Function'daki text_to_speech'in Django karşılığı.
GOOGLE_APPLICATION_CREDENTIALS env değişkeni ayarlıysa çalışır.
"""
import base64
import logging

from django.conf import settings

logger = logging.getLogger("ihaletakip")


class TTSError(Exception):
    def __init__(self, message, status=500):
        super().__init__(message)
        self.message = message
        self.status = status


def synthesize_speech(text: str) -> str:
    """
    Metni MP3'e çevirir ve base64 string döndürür.

    Raises:
        TTSError
    """
    from google.cloud import texttospeech

    text = (text or "").strip()
    if not text:
        raise TTSError("Metin boş.", status=400)

    if len(text) > settings.TTS_MAX_CHARS:
        text = text[: settings.TTS_MAX_CHARS]

    try:
        client = texttospeech.TextToSpeechClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=settings.TTS_LANGUAGE_CODE,
            name=settings.TTS_VOICE_NAME,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
        )
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
    except Exception as e:
        logger.exception("TTS hatası: %s", e)
        raise TTSError("Ses oluşturulurken hata oluştu.") from e

    return base64.b64encode(response.audio_content).decode("utf-8")
