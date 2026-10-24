"""Modelos compartidos: Whisper (oído), LFM2.5-1.2B (cerebro) y Kokoro (voz)."""

import os

import numpy as np
import torch
from faster_whisper import WhisperModel
from kokoro import KPipeline
from transformers import AutoModelForCausalLM, AutoTokenizer

LLM_REPO = "LiquidAI/LFM2.5-1.2B-Instruct"
VOZ = os.environ.get("VOZ", "ef_dora")  # ef_dora (mujer) | em_alex (hombre)

# int8: la GTX 1070 (sm_61) no tiene fp16 rápido para CTranslate2
whisper = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8")
tts = KPipeline(lang_code="e", repo_id="hexgrad/Kokoro-82M", device="cuda")


_llm = None


def cargar_llm():
    """El modelo de texto local solo se carga si se usa (la web sí; Alt+Z usa Claude)."""
    global _llm
    if _llm is None:
        _llm = (AutoTokenizer.from_pretrained(LLM_REPO),
                AutoModelForCausalLM.from_pretrained(LLM_REPO, dtype=torch.bfloat16, device_map="cuda").eval())
    return _llm


def transcribir(wav: np.ndarray) -> str:
    """wav: float32 mono a 16 kHz."""
    segs, _ = whisper.transcribe(wav, language="es", vad_filter=True, beam_size=1)
    return " ".join(s.text for s in segs).strip()


def sintetizar(texto: str, voz: str = VOZ, velocidad: float = 1.0):
    """Genera trozos de audio float32 mono a 24 kHz."""
    for _, _, audio in tts(texto, voice=voz, speed=velocidad):
        if audio is not None:
            yield audio.numpy()
