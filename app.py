"""Asistente de voz en español, 100% local: Whisper (oído) -> LFM2.5-1.2B (cerebro) -> Kokoro (voz)."""

import re
from threading import Thread

import gradio as gr
import numpy as np
from fastrtc import AdditionalOutputs, ReplyOnPause, WebRTC
from transformers import TextIteratorStreamer

from modelos import cargar_llm, sintetizar, transcribir

tok, llm = cargar_llm()

SYSTEM = (
    "Eres un asistente de voz amable que habla español de España. "
    "Responde siempre en español, de forma breve y natural, como en una conversación hablada: "
    "frases cortas, sin listas, sin markdown, sin emojis."
)

FIN_FRASE = re.compile(r"(.+?[.!?¿¡;:…\n])\s", re.S)


def hablar(texto: str):
    for audio in sintetizar(texto):
        yield (24_000, (audio * 32_767).astype(np.int16).reshape(1, -1))


def responder(audio: tuple[int, np.ndarray], _id: str, historial: list[dict]):
    rate, wav = audio
    pregunta = transcribir(wav.astype(np.float32).flatten() / 32_768)
    if not pregunta:
        return
    historial.append({"role": "user", "content": pregunta})
    yield AdditionalOutputs(historial)

    ids = tok.apply_chat_template(
        [{"role": "system", "content": SYSTEM}] + historial[-20:],
        add_generation_prompt=True, return_tensors="pt", return_dict=True,
    ).to("cuda")
    streamer = TextIteratorStreamer(tok, skip_prompt=True, skip_special_tokens=True)
    Thread(target=llm.generate, kwargs=dict(
        **ids, streamer=streamer, max_new_tokens=300, do_sample=True,
        temperature=0.3, min_p=0.15, repetition_penalty=1.05,
    )).start()

    # Habla frase a frase mientras el modelo sigue escribiendo
    historial.append({"role": "assistant", "content": ""})
    pendiente = ""
    for trozo in streamer:
        historial[-1]["content"] += trozo
        pendiente += trozo
        while m := FIN_FRASE.match(pendiente):
            frase, pendiente = m.group(1), pendiente[m.end():]
            yield AdditionalOutputs(historial)
            yield from hablar(frase)
    yield AdditionalOutputs(historial)
    if pendiente.strip():
        yield from hablar(pendiente)


with gr.Blocks(title="Voz en español") as demo:
    gr.Markdown("# Asistente de voz en español (local)\nPulsa el micro, habla y haz una pausa: te contesta.")
    historial = gr.State([])
    webrtc = WebRTC(modality="audio", mode="send-receive", full_screen=False)
    chat = gr.Chatbot(type="messages", label="Conversación", height=400)
    borrar = gr.Button("Nueva conversación")

    webrtc.stream(
        ReplyOnPause(responder, input_sample_rate=16_000, output_sample_rate=24_000, can_interrupt=False),
        inputs=[webrtc, historial],
        outputs=[webrtc],
    )
    webrtc.on_additional_outputs(lambda h: list(h), outputs=[chat])
    borrar.click(lambda: ([], []), outputs=[historial, chat])

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7861)
