"""Modelos compartidos: Nemotron 3.5 o Whisper (oído), LFM2.5-1.2B (cerebro) y Kokoro (voz)."""

import os
from contextlib import ExitStack

import numpy as np
import torch
import transcribe_cpp
from faster_whisper import WhisperModel
from huggingface_hub import hf_hub_download
from kokoro import KPipeline
from transformers import AutoModelForCausalLM, AutoTokenizer

LLM_REPO = "LiquidAI/LFM2.5-1.2B-Instruct"
VOZ = os.environ.get("VOZ", "ef_dora")  # ef_dora (mujer) | em_alex (hombre)

# Oído: el mismo Nemotron 3.5 que usa Handy (transcribe.cpp por Vulkan), en directo y en español.
NEMOTRON = ("handy-computer/nemotron-3.5-asr-streaming-0.6b-gguf", "nemotron-3.5-asr-streaming-0.6b-Q8_0.gguf")
IDIOMA = "es-ES"
try:
    oido = transcribe_cpp.Model(hf_hub_download(*NEMOTRON))
except Exception as e:  # sin el modelo o sin Vulkan: Whisper de respaldo
    print(f"Nemotron no disponible ({e}); uso Whisper", flush=True)
    oido = None

_whisper = None


def cargar_whisper():
    global _whisper
    if _whisper is None:
        # int8: la GTX 1070 (sm_61) no tiene fp16 rápido para CTranslate2
        _whisper = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8")
    return _whisper


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
    if oido:
        with oido.session() as sesion:
            return sesion.run(np.ascontiguousarray(wav, dtype=np.float32), language=IDIOMA).text.strip()
    segs, _ = cargar_whisper().transcribe(wav, language="es", vad_filter=True, beam_size=1)
    return " ".join(s.text for s in segs).strip()


class EnDirecto:
    """Transcribe mientras hablas: feed() con trozos de audio, texto() lo que lleva, final() al acabar."""

    def __init__(self):
        self._pila = ExitStack()
        sesion = self._pila.enter_context(oido.session())
        self._flujo = self._pila.enter_context(sesion.stream(language=IDIOMA))

    def feed(self, trozo: np.ndarray):
        self._flujo.feed(np.ascontiguousarray(trozo, dtype=np.float32))

    def texto(self) -> str:
        return self._flujo.text().display.strip()

    def final(self) -> str:
        self._flujo.finalize()
        return self._flujo.snapshot().text.strip()

    def cerrar(self):
        self._pila.close()


def sintetizar(texto: str, voz: str = VOZ, velocidad: float = 1.0):
    """Genera trozos de audio float32 mono a 24 kHz."""
    for _, _, audio in tts(texto, voice=voz, speed=velocidad):
        if audio is not None:
            yield audio.numpy()
