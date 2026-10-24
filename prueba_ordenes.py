"""Simulacro: qué haría el asistente con cada frase (no ejecuta nada)."""
import sys, time
import asistente as A

ejecutadas = []
A.sh = lambda *c: ejecutadas.append(" ".join(c))
A.abrir_url = lambda u: ejecutadas.append("abrir " + u)
A.subprocess.run = lambda c, **k: ejecutadas.append(" ".join(c))
class Mudo:
    def decir(self, *a): pass
    def terminar(self): pass
A.escribir = lambda l: None

frases = sys.argv[1:] or [
    "abre el navegador", "ábreme Discord", "abre la terminal", "abre WhatsApp", "abre los archivos",
    "pon algo de música relajante en YouTube", "busca en internet el tiempo en Madrid",
    "sube el volumen", "pon el volumen al 30", "silencia el sonido",
    "cierra esta ventana", "vete al escritorio 3", "ponlo en pantalla completa",
    "hazme una captura de pantalla", "recuérdame en 10 minutos sacar la ropa",
    "abre gmail", "qué hora es", "cuéntame un chiste corto", "bloquea la pantalla", "abre visual studio code",
]
SESION = [None]
for f in frases:
    a = A.Asistente(); a.voz = Mudo(); ejecutadas.clear()
    a.historial = [{"role": "user", "content": f}]; a.sesion = SESION[0]
    t = time.time(); r = a.pensar_y_hablar(a.turno); SESION[0] = a.sesion
    print(f"{f!r:45} -> {r!r}  {ejecutadas}  ({time.time()-t:.1f}s)")
