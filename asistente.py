"""Asistente de voz de escritorio, 100% local.

Alt+Z toque      -> conversación: escucha, responde y vuelve a escuchar; otro toque la termina y cierra la ventanita.
Alt+Z mantenido  -> pulsar para hablar: una sola orden mientras lo tienes pulsado.

Hyprland escribe "down"/"up" en una FIFO (voz-es-ctl); este proceso graba con
pw-record, transcribe con Whisper, decide con LFM2.5 (con herramientas para
controlar el escritorio) y contesta con Kokoro por pw-play.
"""

import ast
import configparser
import datetime
import difflib
import getpass
import json
import traceback
import os
import pwd
import fcntl
import re
import shutil
import struct
import subprocess
import threading
import time
import unicodedata
import urllib.parse
import uuid
from pathlib import Path

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps
from modelos import VOZ, EnDirecto, oido, sintetizar, transcribir

FIFO = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voz-es.fifo"
AJUSTES = Path.home() / ".config/voz-es/ajustes.json"
NIVEL = FIFO.with_name("voz-es.nivel")  # nivel de audio (mic o voz) que lee la aura para moverse con el sonido
SILENCIADO = FIFO.with_name("voz-es.silenciado")  # marca: los altavoces los silenció el asistente
LOG = Path.home() / ".local/state/voz-es/conversacion.log"
PANEL = "org.omarchy.voz-es"
CLAUDE_DIR = Path.home() / ".local/state/voz-es/claude"  # carpeta vacía: Claude no hurga en el proyecto
SILENCIO_FIN = 1.0  # s de silencio tras hablar para cerrar tu turno
INACTIVIDAD = 60  # s sin que hables para terminar la conversación sola
NUEVA = re.compile(r"\b(nueva (conversacion|sesion)|(empieza(mos)?|empecemos|empezamos) de (cero|nuevo)|olvida (todo|la conversacion)|borra la conversacion)\b")
OLVIDO = 300  # s sin hablar tras los que se olvida la conversación

USUARIO = getpass.getuser()
try:  # cómo te llama: «nombre» en ajustes.json, si no el nombre completo del sistema
    NOMBRE = json.loads(AJUSTES.read_text()).get("nombre", "")
except (FileNotFoundError, json.JSONDecodeError):
    NOMBRE = ""
NOMBRE = NOMBRE or pwd.getpwnam(USUARIO).pw_gecos.split(",")[0] or USUARIO

SYSTEM = (
    f"Eres el asistente de voz del ordenador de {NOMBRE}: Arch Linux con Omarchy y Hyprland 0.56 (configuración en Lua), "
    f"usuario {USUARIO}, home {Path.home()}. Te habla por voz y tu respuesta se lee en voz alta con un sintetizador: "
    "responde siempre en español de España, en una o dos frases cortas, sin markdown, sin listas, sin emojis, sin rutas "
    "ni comandos en el texto salvo que te los pida.\n"
    "Si te pide hacer algo en el ordenador, hazlo con Bash sin preguntar y confirma en pocas palabras. Pistas:\n"
    "- Abrir apps: busca su .desktop en ~/.local/share/applications o /usr/share/applications y lanza "
    "'setsid -f uwsm-app -- NOMBRE.desktop'. Webs: 'setsid -f omarchy-launch-browser URL'.\n"
    "- Ventanas: hyprctl dispatch 'hl.dsp.window.close()', hyprctl dispatch 'hl.dsp.focus({{ workspace = \"3\" }})', hyprctl clients -j.\n"
    "- Omarchy: 'omarchy commands' lista todo (volumen, capturas, temas, recordatorios, bluetooth...).\n"
    "- Información del sistema: free, df, lscpu, nvidia-smi, ps, sensors, ip.\n"
    "Nunca hagas nada destructivo o difícil de deshacer (borrar archivos, apagar, reiniciar, instalar o desinstalar, sudo, "
    "cambiar configuraciones importantes): explica en una frase que eso mejor lo haga él a mano.\n"
    "Ahora es {ahora}."
)

# Lo que Claude puede hacer sin preguntar, y lo que nunca puede por voz
PERMITIDAS = ["Bash", "Read", "Glob", "Grep", "WebSearch", "WebFetch"]
PROHIBIDAS = ["Edit", "Write", "NotebookEdit"] + [f"Bash({c}:*)" for c in (
    "rm", "rmdir", "shred", "dd", "mkfs", "sudo", "pkexec", "su", "doas", "shutdown", "reboot", "poweroff", "halt",
    "systemctl poweroff", "systemctl reboot", "systemctl suspend", "omarchy system", "omarchy pkg", "omarchy install",
    "omarchy refresh", "omarchy reinstall", "omarchy update", "pacman", "yay", "paru", "pip", "npm", "git push",
    "git reset", "chmod", "chown", "mv", "truncate", "crontab", "curl", "wget")]


# ---------- utilidades ----------

def normalizar(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[^a-z0-9 ]", "", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


def sh(*cmd: str):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def ajustes() -> dict:
    """Los que guarda la app «Asistente de voz» (~/voz-es/ajustes.py)."""
    try:
        return json.loads(AJUSTES.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def escribir(linea: str):
    # \r + borrar línea: sustituye lo que estuviera a medias (el texto en directo)
    with LOG.open("a") as f:
        f.write("\r\033[2K" + linea + "\n")


def escribir_parcial(texto: str):
    """Lo que vas diciendo, en la última línea de la ventanita (sin salto: se va reescribiendo)."""
    texto = texto if len(texto) <= 40 else "…" + texto[-39:]
    with LOG.open("a") as f:
        f.write(f"\r\033[2K\033[36m> {texto}\033[0m")


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


def ahora() -> str:
    t = datetime.datetime.now()
    return f"{DIAS[t.weekday()]} {t.day} de {MESES[t.month - 1]} de {t.year}, las {t:%H:%M}"


# ---------- aplicaciones instaladas ----------

ALIAS = {
    "navegador": "omarchy-browser", "internet": "omarchy-browser", "chrome": "omarchy-browser",
    "chromium": "omarchy-browser", "terminal": "__terminal__", "consola": "__terminal__",
    "archivos": "org.gnome.Nautilus", "explorador de archivos": "org.gnome.Nautilus",
}


def indexar_apps() -> dict[str, str]:
    """nombre normalizado -> id de la entrada .desktop"""
    dirs = [Path.home() / ".local/share/applications", Path("/usr/share/applications"),
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications"]
    apps: dict[str, str] = {}
    for d in reversed(dirs):  # las del usuario tienen prioridad
        for f in d.glob("*.desktop") if d.is_dir() else []:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay") == "true" or e.get("Hidden") == "true" or e.get("Type") != "Application":
                continue
            for k in ("Name", "Name[es]", "GenericName[es]", "GenericName"):
                if e.get(k):
                    apps[normalizar(e[k])] = f.stem
            apps.setdefault(normalizar(f.stem.split(".")[-1]), f.stem)
    return apps


def buscar_app(nombre: str) -> str | None:
    n = normalizar(nombre)
    if n in ALIAS:
        return ALIAS[n]
    apps = indexar_apps()
    if n in apps:
        return apps[n]
    for k, v in apps.items():
        if n and (n in k.split() or k in n.split() or k.startswith(n)):
            return v
    m = difflib.get_close_matches(n, list(apps), n=1, cutoff=0.6)
    return apps[m[0]] if m else None


# ---------- herramientas ----------

WEBS = {"gmail": "https://mail.google.com", "correo": "https://mail.google.com",
        "google": "https://www.google.com", "youtube": "https://www.youtube.com",
        "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai",
        "github": "https://github.com", "amazon": "https://www.amazon.es",
        "wikipedia": "https://es.wikipedia.org", "netflix": "https://www.netflix.com"}


def abrir_url(url: str):
    sh("omarchy-launch-browser", url) if subprocess.run(["which", "omarchy-launch-browser"], capture_output=True).returncode == 0 \
        else sh("xdg-open", url)


def ejecutar(nombre: str, args: dict) -> str:
    """Ejecuta una herramienta y devuelve una confirmación para decir en voz alta."""
    match nombre:
        case "abrir_aplicacion":
            app = buscar_app(args.get("nombre", ""))
            if not app:
                return f"No encuentro ninguna aplicación llamada {args.get('nombre')}."
            if app == "__terminal__":
                sh("uwsm-app", "--", "xdg-terminal-exec")
            else:
                sh("uwsm-app", "--", f"{app}.desktop")
            return f"Abriendo {args['nombre']}."
        case "abrir_web":
            sitio = args.get("sitio", "").strip()
            n = normalizar(sitio).replace(" ", "")
            if n in WEBS:
                url = WEBS[n]
            elif "." in sitio:
                url = sitio if sitio.startswith("http") else "https://" + sitio.replace(" ", "")
            else:
                url = "https://www.google.com/search?btnI=1&q=" + urllib.parse.quote(sitio)
            abrir_url(url)
            return f"Abriendo {sitio}."
        case "buscar_en_internet":
            abrir_url("https://www.google.com/search?q=" + urllib.parse.quote(args.get("consulta", "")))
            return "Buscando."
        case "buscar_en_youtube":
            abrir_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote(args.get("consulta", "")))
            return "Buscando en YouTube."
        case "volumen":
            accion = args.get("accion")
            if accion == "fijar" and args.get("nivel") is not None:
                nivel = max(0, min(100, int(args["nivel"])))
                sh("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{nivel}%")
                return f"Volumen al {nivel} por ciento."
            orden = {"subir": "raise", "bajar": "lower", "silenciar": "mute-toggle"}.get(accion)
            if not orden:
                return "No sé qué hacer con el volumen."
            for _ in range(1 if orden == "mute-toggle" else 2):
                subprocess.run(["omarchy", "audio", "output", "volume", orden], capture_output=True)
            return "Hecho."
        case "cerrar_ventana":
            sh("hyprctl", "dispatch", "hl.dsp.window.close()")
            return "Cerrada."
        case "pantalla_completa":
            sh("hyprctl", "dispatch", 'hl.dsp.window.fullscreen({ mode = "fullscreen" })')
            return "Hecho."
        case "cambiar_escritorio":
            n = int(args.get("numero", 1))
            sh("hyprctl", "dispatch", f'hl.dsp.focus({{ workspace = "{n}" }})')
            return f"Escritorio {n}."
        case "captura_de_pantalla":
            sh("omarchy", "capture", "screenshot", "fullscreen", "save")
            return "Captura hecha."
        case "recordatorio":
            minutos = int(args.get("minutos", 5))
            sh("omarchy", "reminder", str(minutos), args.get("mensaje", "Recordatorio"))
            return f"Te aviso en {minutos} minutos."
        case "bloquear_pantalla":
            sh("omarchy", "system", "lock")
            return "Bloqueando."
        case "luz_nocturna":
            sh("omarchy", "toggle", "nightlight")
            return "Hecho."
    return "No sé hacer eso todavía."


def gb(n_bytes: float) -> str:
    return f"{n_bytes / 2**30:.1f}".replace(".", ",").replace(",0", "") + " gigas"


def sistema() -> dict[str, str]:
    """Datos del ordenador en vivo, ya redactados para decirlos en voz alta."""
    mem = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo")}
    total, libre = mem["MemTotal"], mem["MemAvailable"]
    cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "desconocido")
    carga = os.getloadavg()[0]
    disco = shutil.disk_usage(Path.home())
    segundos = float(open("/proc/uptime").read().split()[0])
    h, m = int(segundos // 3600), int(segundos % 3600 // 60)
    try:
        nombre, usada, total_gpu, temp = [x.strip() for x in subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3).stdout.split(",")]
        gpu = f"La gráfica es una {nombre.replace('NVIDIA ', '')}, usa {gb(int(usada) * 2**20)} de {gb(int(total_gpu) * 2**20)} de memoria y está a {temp} grados."
    except Exception:
        gpu = "No puedo leer la tarjeta gráfica."
    return {
        "ram": f"Tienes {gb(total)} de RAM; ahora estás usando {gb(total - libre)} y quedan {gb(libre)} libres.",
        "cpu": f"El procesador es un {re.sub(r'\s+', ' ', cpu)} con {os.cpu_count()} hilos y ahora está al "
               f"{min(100, round(carga / os.cpu_count() * 100))} por ciento.",
        "gpu": gpu,
        "disco": f"En el disco te quedan {gb(disco.free)} libres de {gb(disco.total)}.",
        "encendido": f"El ordenador lleva encendido {h} horas y {m} minutos." if h else f"El ordenador lleva encendido {m} minutos.",
    }


NUMEROS = {"uno": 1, "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
           "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "quince": 15, "veinte": 20, "treinta": 30,
           "media": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "cien": 100}
NUM = r"(\d+|" + "|".join(NUMEROS) + r")"
ARTICULO = r"(?:el|la|los|las|un|una|mi|me|nos)\s+"


def numero(s: str) -> int:
    return int(s) if s.isdigit() else NUMEROS[s]


def orden_directa(texto: str) -> list[tuple[str, dict]] | str | None:
    """Reconoce las órdenes habituales sin pasar por el modelo (más rápido y fiable).

    Devuelve llamadas a herramientas, una respuesta hablada, o None si no es una orden conocida.
    """
    t = normalizar(texto)
    t = re.sub(r"^(?:oye |eh |por favor |puedes |podrias |quiero que )+", "", t)
    t = re.sub(r" por favor$", "", t)

    if re.search(r"\bvolumen\b|\bsonido\b|\bsilencia|\bmutea|\bmute\b", t):
        if m := re.search(r"\bal? " + NUM + r"\b", t):
            return [("volumen", {"accion": "fijar", "nivel": numero(m.group(1))})]
        if re.search(r"\b(sube|subir|subelo|aumenta|mas alto|mas fuerte)\b", t):
            return [("volumen", {"accion": "subir"})]
        if re.search(r"\b(baja|bajar|bajalo|disminuye|mas bajo|menos)\b", t):
            return [("volumen", {"accion": "bajar"})]
        if re.search(r"silencia|mutea|mute|quita el sonido|activa el sonido|pon el sonido", t):
            return [("volumen", {"accion": "silenciar"})]
    temas = [("ram", r"\bram\b|\bmemoria(?! de video| de la grafica)"),
             ("gpu", r"grafica|\bgpu\b|tarjeta de video|nvidia|memoria de video"),
             ("cpu", r"procesador|\bcpu\b|\bcores?\b|\bnucleos\b"),
             ("disco", r"\bdisco\b|almacenamiento|espacio libre|cuanto espacio"),
             ("encendido", r"cuanto tiempo lleva encendido|lleva encendido|\buptime\b")]
    pedidos = [k for k, patron in temas if re.search(patron, t)]
    if re.search(r"\b(especificaciones|caracteristicas|que ordenador|que equipo|componentes|hardware)\b", t):
        pedidos = ["cpu", "ram", "gpu", "disco"]
    # Lo que vaya más allá de "cuánto tengo / cuánto queda" (qué programa, por qué, cómo liberar...) lo responde Claude
    complejo = r"\b(programas?|procesos?|aplicacion(es)?|apps?|cual|quien|por que|porque|como|mas|libera|limpia|optimiza)\b"
    if pedidos and not re.search(r"^(abre|abreme|lanza|busca|buscame)\b", t) and not re.search(complejo, t):
        datos = sistema()
        return " ".join(datos[k] for k in pedidos)
    if re.search(r"\bque hora es\b|\bdime la hora\b", t):
        return f"Son las {datetime.datetime.now():%H:%M}."
    if re.search(r"\bque dia es\b|\ba que estamos\b|\bque fecha\b", t):
        return f"Hoy es {ahora().split(',')[0]}."
    if re.search(r"\bcierra (esta |la |esa )?ventana\b|^cierra (esto|eso)$", t):
        return [("cerrar_ventana", {})]
    if m := re.search(r"\b(?:escritorio|workspace|espacio de trabajo)(?: numero)? " + NUM + r"\b", t):
        return [("cambiar_escritorio", {"numero": numero(m.group(1))})]
    if re.search(r"pantalla completa", t):
        return [("pantalla_completa", {})]
    if re.search(r"captura|pantallazo|screenshot", t):
        return [("captura_de_pantalla", {})]
    if re.search(r"\bbloquea(r)? (la pantalla|el ordenador|el equipo|el pc)\b", t):
        return [("bloquear_pantalla", {})]
    if re.search(r"luz nocturna|modo nocturno|filtro de luz azul", t):
        return [("luz_nocturna", {})]
    if m := re.search(r"\b(?:recuerdame|avisame|recordatorio)\b.*?\ben " + NUM + r" (minutos?|horas?)\b(?: (?:de |que |para )?(.*))?", t):
        minutos = numero(m.group(1)) * (60 if m.group(2).startswith("hora") else 1)
        mensaje = (m.group(3) or "").strip() or re.sub(r"\b(recuerdame|avisame)\b|\ben .*", "", t).strip() or "Recordatorio"
        return [("recordatorio", {"minutos": minutos, "mensaje": mensaje.capitalize()})]
    if m := re.search(r"^(?:pon|ponme|busca|buscame|reproduce|quiero ver|quiero escuchar)\s+(.+?)\s+(?:en|de) youtube$", t):
        return [("buscar_en_youtube", {"consulta": re.sub(r"^(?:algo de|un video de|videos? de)\s+", "", m.group(1))})]
    if m := re.search(r"^busca(?:r|me)?\s+(?:en (?:internet|google)\s+)?(.+?)(?:\s+en (?:internet|google))?$", t):
        return [("buscar_en_internet", {"consulta": m.group(1)})]
    if m := re.search(r"^(?:abre|abreme|abrir|lanza|inicia|arranca|ejecuta|ensename)\s+(?:" + ARTICULO + r")?(.+)$", t):
        objetivo = m.group(1)
        if dominio := re.search(r"\b[\w-]+(?: ?(?:\.|punto) ?)(?:com|es|org|net|io|ai|dev|tv)\b", texto.lower()):
            return [("abrir_web", {"sitio": re.sub(r" ?punto ?| ", ".", dominio.group(0)).replace("..", ".")})]
        if objetivo.replace(" ", "") in WEBS and not buscar_app(objetivo):
            return [("abrir_web", {"sitio": objetivo})]
        return [("abrir_aplicacion", {"nombre": objetivo})]
    return None


# ---------- audio ----------

_nivel_fd = None


def publicar_nivel(x: np.ndarray):
    """Escribe el volumen (0..1, escala dB) de un trozo de audio en un fichero de 12 bytes que lee aura.py.
    Lleva la hora: si deja de actualizarse, la aura lo da por silencio."""
    global _nivel_fd
    if not x.size:
        return
    try:
        if _nivel_fd is None:
            _nivel_fd = os.open(NIVEL, os.O_WRONLY | os.O_CREAT, 0o600)
        rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float32)))) + 1e-9
        nivel = min(1.0, max(0.0, (20 * np.log10(rms) + 55) / 35))  # -55 dB → 0, -20 dB → 1
        os.pwrite(_nivel_fd, struct.pack("<df", time.monotonic(), nivel), 0)
    except OSError:
        pass


class Grabadora:
    def __init__(self):
        self.proc = None
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.silenciado = False

    def empezar(self):
        self.buf = bytearray()
        # Silencia los altavoces mientras escucha para que no se cuele un vídeo o música
        estado = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True).stdout
        self.silenciado = "MUTED" not in estado
        if self.silenciado:
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"])
            SILENCIADO.touch()
        self.proc = subprocess.Popen(
            ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "--raw", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._leer, args=(self.proc,), daemon=True).start()

    def _leer(self, proc):
        while chunk := proc.stdout.read1(3200):
            with self.lock:
                self.buf += chunk
            publicar_nivel(np.frombuffer(chunk[: len(chunk) // 2 * 2], np.int16).astype(np.float32) / 32_768)

    def audio(self) -> np.ndarray:
        with self.lock:
            return np.frombuffer(bytes(self.buf), np.int16).astype(np.float32) / 32_768

    def parar(self) -> np.ndarray:
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None
        if self.silenciado:
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
            SILENCIADO.unlink(missing_ok=True)
            self.silenciado = False
        return self.audio()


def _encadenar(primero, resto):
    if primero is not None:
        yield primero
    yield from resto


class Altavoz:
    def __init__(self):
        self.proc = None
        self.sonando = None

    def decir(self, texto: str, turno: int, asistente: "Asistente"):
        texto = re.sub(r"[^\u0000-⓿]", "", texto).strip()  # fuera emojis
        if not texto:
            return
        aj = ajustes()
        try:
            trozos = sintetizar(texto, aj.get("voz", VOZ), aj.get("velocidad", 1.0))
            primero = next(trozos, None)
        except Exception as e:  # p. ej. una voz que no está descargada: habla con la de siempre
            print(f"voz {aj.get('voz')!r} falla ({e}); uso {VOZ}", flush=True)
            trozos = sintetizar(texto, VOZ, aj.get("velocidad", 1.0))
            primero = next(trozos, None)
        for audio in _encadenar(primero, trozos):
            if asistente.turno != turno:
                return
            if not self.proc or self.proc.poll() is not None:
                self.proc = subprocess.Popen(
                    ["pw-play", "--rate", "24000", "--channels", "1", "--format", "f32", "--raw", "-"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:  # tubería mínima: así escribir a trozos va al ritmo de lo que suena y la aura lo sigue
                    fcntl.fcntl(self.proc.stdin.fileno(), 1031, 4096)  # F_SETPIPE_SZ
                except OSError:
                    pass
            try:
                audio = audio.astype(np.float32)
                for i in range(0, len(audio), 1200):  # trozos de 50 ms
                    if asistente.turno != turno:
                        return
                    trozo = audio[i:i + 1200]
                    self.proc.stdin.write(trozo.tobytes())
                    self.proc.stdin.flush()
                    publicar_nivel(trozo)
            except BrokenPipeError:
                return

    def terminar(self):
        """Cierra la entrada; pw-play acaba de reproducir lo que tenga."""
        if self.proc and self.proc.stdin:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
        self.sonando, self.proc = self.proc, None

    def esperar(self, turno: int, asistente: "Asistente"):
        """Espera a que acabe de hablar (para no escucharse a sí mismo)."""
        while self.sonando and self.sonando.poll() is None and asistente.turno == turno:
            time.sleep(0.05)

    def callar(self):
        for p in (self.proc, self.sonando):
            if p and p.poll() is None:
                p.kill()
        self.proc = self.sonando = None


def pitido(freq: float):
    sr = 24_000
    t = np.arange(int(sr * 0.09)) / sr
    tono = (0.15 * np.sin(2 * np.pi * freq * t) * np.minimum(1, (t[-1] - t) * 60)).astype(np.float32)
    p = subprocess.Popen(["pw-play", "--rate", str(sr), "--channels", "1", "--format", "f32", "--raw", "-"],
                         stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p.communicate(tono.tobytes())


# ---------- asistente ----------

FIN_FRASE = re.compile(r"(.+?[.!?;:…\n])\s", re.S)


class Asistente:
    def __init__(self):
        self.grab = Grabadora()
        self.voz = Altavoz()
        self.lock = threading.Lock()
        self.conversando = False  # modo continuo: escucha, responde y vuelve a escuchar
        self.turno = 0  # cada pulsación lo incrementa y cancela lo que estuviera en marcha
        self.historial: list[dict] = []
        self.ultimo = 0.0
        self.sesion: str | None = None  # sesión de Claude
        self.claude: subprocess.Popen | None = None

    def _cancelar(self):
        self.turno += 1
        self.voz.callar()
        self.grab.parar()
        if self.claude and self.claude.poll() is None:
            self.claude.kill()

    # --- eventos de la tecla ---
    def down(self):
        """Pulsar Alt+Z: si está parado empieza a conversar; si ya conversa, termina y cierra.
        Todo se decide al pulsar: Hyprland no siempre avisa al soltar (p. ej. si sueltas Alt antes)."""
        with self.lock:
            self._cancelar()
            if self.conversando:
                self.conversando = False
                escribir("\033[2m— fin —\033[0m")
                cerrar_panel()
                return
            self.conversando = True
            self.historial, self.sesion = [], None  # cada Alt+Z empieza una sesión nueva de Claude
            escribir("\033[2m— conversación (Alt+Z para terminar) —\033[0m")
            threading.Thread(target=self._conversar, args=(self.turno,), daemon=True).start()
        mostrar_panel()

    def _conversar(self, turno: int):
        while self.conversando and self.turno == turno:
            escuchado = self._escuchar(turno)
            if escuchado is None:
                return
            try:
                self.procesar(escuchado[0], turno, escuchado[1])
            except Exception:  # que un fallo suelto no acabe con la conversación
                traceback.print_exc()
                escribir("\033[2m   (algo ha fallado; sigo escuchando)\033[0m")
            self.voz.esperar(turno, self)

    def _escuchar(self, turno: int) -> tuple[np.ndarray, str | None] | None:
        """Graba hasta que dejas de hablar y devuelve (audio, texto). None si se cancela o si pasa
        mucho rato sin que hables. Con Nemotron el texto sale en directo en la ventanita."""
        pitido(880)
        with self.lock:
            if self.turno != turno:
                return None
            self.grab.empezar()
        escribir("\033[2m🎙 escuchando…\033[0m")
        vad = VadOptions(min_silence_duration_ms=300, speech_pad_ms=100)
        directo = EnDirecto() if oido else None
        enviado, mostrado = 0, ""
        try:
            while True:
                time.sleep(0.2)
                if self.turno != turno:
                    return None
                audio = self.grab.audio()
                dur = len(audio) / 16_000
                if directo and len(audio) > enviado:
                    directo.feed(audio[enviado:])
                    enviado = len(audio)
                    if (t := directo.texto()) and t != mostrado:
                        escribir_parcial(mostrado := t)
                voz = get_speech_timestamps(audio, vad) if dur > 0.5 else []
                if (voz and (len(audio) - voz[-1]["end"]) / 16_000 > SILENCIO_FIN) or dur > 60:
                    with self.lock:
                        if self.turno != turno:
                            return None
                        audio = self.grab.parar()
                    pitido(660)
                    if directo:
                        directo.feed(audio[enviado:])
                        return audio, directo.final()
                    return audio, None
                if not voz and dur > INACTIVIDAD:
                    with self.lock:
                        if self.turno != turno:
                            return None
                        self.grab.parar()
                        self.conversando = False
                    escribir("\033[2m— fin (sin actividad) —\033[0m")
                    cerrar_panel()
                    return None
        finally:
            if directo:
                directo.cerrar()

    # --- cerebro ---
    def procesar(self, audio: np.ndarray, turno: int, texto: str | None = None):
        if len(audio) < 16_000 * 0.3:
            return
        if texto is None:
            texto = transcribir(audio)
        if not texto:
            escribir("\033[2m   (no te he entendido)\033[0m")
            return
        escribir(f"\033[1;36m> {texto}\033[0m")
        if time.monotonic() - self.ultimo > OLVIDO:
            self.historial, self.sesion = [], None
        self.ultimo = time.monotonic()
        if NUEVA.search(normalizar(texto)):
            self.historial, self.sesion = [], None
            escribir("\033[2m— sesión nueva —\033[0m")
            self.voz.decir("Vale, empezamos de cero.", turno, self)
            self.voz.terminar()
            return
        self.historial.append({"role": "user", "content": texto})

        respuesta = self.pensar_y_hablar(turno)
        self.historial.append({"role": "assistant", "content": respuesta})
        self.historial = self.historial[-12:]

    def pensar_y_hablar(self, turno: int) -> str:
        directa = orden_directa(self.historial[-1]["content"])
        if directa is not None:
            if isinstance(directa, str):
                texto = directa
            else:
                texto = self.hacer(directa)
            escribir(texto)
            self.voz.decir(texto, turno, self)
            self.voz.terminar()
            return texto

        return self.preguntar_a_claude(turno)

    def preguntar_a_claude(self, turno: int, reintento: bool = False) -> str:
        cmd = ["claude", "-p", "--model", "haiku", "--output-format", "stream-json", "--verbose",
               "--strict-mcp-config", "--append-system-prompt", SYSTEM.format(ahora=ahora()),
               "--allowedTools", *PERMITIDAS, "--disallowedTools", *PROHIBIDAS]
        if self.sesion:
            cmd += ["--resume", self.sesion]
        else:
            self.sesion = str(uuid.uuid4())
            cmd += ["--session-id", self.sesion]
        escribir("\033[2m   … pensando (Claude)\033[0m")
        proc = self.claude = subprocess.Popen(cmd + ["--", self.historial[-1]["content"]], stdout=subprocess.PIPE,
                                              stderr=subprocess.DEVNULL, text=True, cwd=CLAUDE_DIR)
        threading.Timer(90, proc.kill).start()
        dicho = []
        for linea in proc.stdout:
            try:
                ev = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if self.turno != turno:
                proc.kill()
                break
            if ev.get("type") == "assistant":
                for b in ev["message"].get("content", []):
                    if b.get("type") == "tool_use":
                        detalle = b["input"].get("command") or b["input"].get("query") or b["input"].get("url") or ""
                        escribir(f"\033[2m   ⚙ {b['name']}: {detalle[:70]}\033[0m")
                    elif b.get("type") == "text" and b["text"].strip():
                        texto = re.sub(r"[*_`#>]", "", b["text"]).strip()
                        dicho.append(texto)
                        escribir(texto)
                        self.voz.decir(texto, turno, self)
            elif ev.get("type") == "result" and ev.get("is_error"):
                escribir(f"\033[2m   (error de Claude: {str(ev.get('result'))[:80]})\033[0m")
        proc.wait()
        if not dicho and proc.returncode and self.turno == turno and not reintento:
            self.sesion = None  # la sesión anterior quedó rota (p. ej. cortada a medias): empieza otra
            return self.preguntar_a_claude(turno, reintento=True)
        if not dicho and self.turno == turno:
            texto = "No he podido contactar con Claude." if proc.returncode else "Hecho."
            escribir(texto)
            self.voz.decir(texto, turno, self)
            dicho.append(texto)
        self.voz.terminar()
        return " ".join(dicho)

    def hacer(self, llamadas: list[tuple[str, dict]]) -> str:
        confirmaciones = []
        for nombre, args in llamadas:
            escribir(f"\033[2m   ⚙ {nombre}({json.dumps(args, ensure_ascii=False)})\033[0m")
            try:
                confirmaciones.append(ejecutar(nombre, args))
            except Exception as e:  # una orden rota no debe tumbar el asistente
                confirmaciones.append(f"Ha fallado: {e}")
        return " ".join(confirmaciones) or "No he podido hacerlo."


def mostrar_panel():
    modo = ajustes().get("modo", "ventanita")  # «ventanita», «aura» o «ambos»
    if modo in ("aura", "ambos"):
        sh(str(Path.home() / ".local/bin/voz-es-aura"))
    if modo == "aura":
        return
    try:
        clientes = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout)
    except json.JSONDecodeError:
        clientes = []
    if not any(c.get("class") == PANEL for c in clientes):
        sh("omarchy-launch-tui", f"--app-id={PANEL}", str(Path.home() / ".local/bin/voz-es-panel"))


def cerrar_panel():
    # pkill y no hyprctl: así también cierra una ventanita que aún no ha terminado de abrirse
    subprocess.run(["pkill", "-f", f"app-id={PANEL}"])
    subprocess.run(["pkill", "-TERM", "-f", "voz-es/aura.py"])  # la aura se apaga con un fundido y sale


def main():
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 200_000:
        LOG.write_text("")
    if not FIFO.exists():
        os.mkfifo(FIFO, 0o600)
    if SILENCIADO.exists():  # se cerró mientras escuchaba: devuelve el sonido
        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])
        SILENCIADO.unlink()
    cerrar_panel()  # por si quedó abierta de antes
    a = Asistente()
    escribir("\033[2m— asistente listo (Alt+Z) —\033[0m")
    # O_RDWR: la FIFO nunca da EOF aunque no haya escritores
    with open(os.open(FIFO, os.O_RDWR), "r") as f:
        for linea in f:
            match linea.strip():
                case "down":
                    a.down()


if __name__ == "__main__":
    main()
