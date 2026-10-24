import time, numpy as np, soundfile as sf, librosa
import app
from fastrtc import AdditionalOutputs
# Pregunta sintetizada con la voz masculina -> 16 kHz como el micro
q = np.concatenate([a for a in app.sintetizar("Hola, ¿qué tiempo suele hacer en Madrid en otoño?", voz="em_alex")])
q16 = (librosa.resample(q, orig_sr=24000, target_sr=16000) * 32767).astype(np.int16).reshape(1, -1)
t0, primero, chunks, hist = time.time(), None, [], []
for out in app.responder((16000, q16), "prueba", hist):
    if not isinstance(out, AdditionalOutputs):
        primero = primero or time.time() - t0; chunks.append(out[1].flatten())
total = time.time() - t0
wav = np.concatenate(chunks)
sf.write("respuesta.wav", wav, 24000)
print("TÚ :", hist[0]["content"]); print("IA :", hist[1]["content"])
print(f"primera voz en {primero:.1f}s | {len(wav)/24000:.1f}s de audio en {total:.1f}s")
