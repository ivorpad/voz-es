#!/usr/bin/env python3
"""Aura de colores alrededor de la pantalla (al estilo del nuevo Siri) para el asistente de voz.

Superficie layer-shell a pantalla completa, transparente y que deja pasar el ratón. Aparece con un
fundido (ease-out) al lanzarla y, con SIGTERM/SIGINT, se apaga con otro fundido (ease-in-out) y sale.
Usa el Python del sistema (GTK4 + gtk4-layer-shell); lo lanza asistente.py según «modo» en ajustes.json.
"""

import json
import math
import os
import signal
import struct
import time
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, GLibUnix, Graphene, Gsk, Gtk, Gtk4LayerShell as Capa  # noqa: E402

AJUSTES = Path.home() / ".config/voz-es/ajustes.json"
NIVEL = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "voz-es.nivel"  # lo escribe asistente.py: (hora, volumen 0..1)
ENTRADA, SALIDA = 0.55, 0.35  # segundos
COLORES = ["#ff9a3c", "#ff4f8b", "#b45cff", "#5b7cff", "#3cd7ff", "#5bffb0", "#ff9a3c"]  # cierra el círculo
# (rotación inicial, vueltas por segundo): dos capas girando a distinto ritmo y sentido dan el flujo
CAPAS = [(0.0, 0.10), (180.0, -0.07)]


def ease_out_expo(t: float) -> float:
    return 1.0 if t >= 1 else 1 - 2 ** (-10 * t)


def ease_in_out_cubic(t: float) -> float:
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def color(hex_: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(hex_)
    return c


class Aura(Gtk.Widget):
    def __init__(self, reducir: bool):
        super().__init__()
        self.reducir = reducir
        self.t0 = None
        self.nivel = 0.0  # intensidad actual 0..1
        self.saliendo = False
        self.nivel_salida = 1.0  # desde dónde arranca el fundido de salida (por si se corta la entrada)
        self.t_salida = 0.0
        self.al_terminar = None
        self.sonido = 0.0  # volumen suavizado 0..1 (mic mientras escuchas, voz mientras habla)
        self.giro = 0.0  # fase acumulada del giro de colores (se acelera con el sonido)
        self.t_ant = None
        self.fd = None
        self.paradas = [Gsk.ColorStop() for _ in COLORES]
        for p, (i, h) in zip(self.paradas, enumerate(COLORES)):
            p.offset, p.color = i / (len(COLORES) - 1), color(h)
        self.add_tick_callback(self._tick)

    def apagar(self, al_terminar):
        if self.saliendo:
            return
        self.saliendo, self.al_terminar = True, al_terminar
        self.nivel_salida = self.nivel
        self.t_salida = self.t_actual

    def _leer_sonido(self) -> float:
        try:
            if self.fd is None:
                self.fd = os.open(NIVEL, os.O_RDONLY)
            hora, nivel = struct.unpack("<df", os.pread(self.fd, 12, 0))
        except (OSError, struct.error):
            return 0.0
        return nivel if time.monotonic() - hora < 0.25 else 0.0  # sin datos recientes = silencio

    def _tick(self, _w, reloj):
        ahora = reloj.get_frame_time() / 1e6
        if self.t0 is None:
            self.t0 = ahora
        dt = 0.0 if self.t_ant is None else min(0.1, ahora - self.t_ant)
        self.t_ant = ahora
        # Ataque rápido y caída lenta, como un medidor de nivel: el brillo salta con la voz y se relaja
        objetivo = 0.0 if self.reducir else self._leer_sonido()
        k = 1 - math.exp(-dt / (0.04 if objetivo > self.sonido else 0.22))
        self.sonido += (objetivo - self.sonido) * k
        self.giro += dt * (1 + 2.5 * self.sonido)
        self.t_actual = ahora
        t = ahora - self.t0
        if self.saliendo:
            # Baja desde el nivel que tuviera, sin saltos aunque se apague a media entrada
            p = min(1.0, (ahora - self.t_salida) / SALIDA)
            self.nivel = self.nivel_salida * (1 - ease_in_out_cubic(p))
            if p >= 1 and self.al_terminar:
                self.al_terminar()
                self.al_terminar = None
        else:
            self.nivel = ease_out_expo(min(1.0, t / ENTRADA))
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def do_snapshot(self, s: Gtk.Snapshot):
        w, h = self.get_width(), self.get_height()
        if self.nivel <= 0.003 or not w:
            return
        t = 0.0 if self.reducir else self.t_actual - self.t0
        # Respiración suave (0.85–1) una vez abierta
        respira = 1.0 if self.reducir else 0.925 + 0.075 * math.sin(t * math.tau / 2.4)
        n = self.nivel * respira
        v = self.sonido
        caja = Graphene.Rect().init(0, 0, w, h)
        radio = min(w, h) * 0.01
        # La luz entra desde el borde: el grosor del anillo crece con el easing de entrada
        grosor = 6 + 14 * self.nivel
        # Cada capa crece con el sonido en su propia fase: así la luz ondula en vez de hincharse toda a la vez
        for i, (ancho, desenfoque, alfa) in enumerate(
                [(grosor * 2.2, 46, 0.55), (grosor, 22, 0.8), (grosor * 0.45, 5, 1.0)]):
            onda = 0.65 + 0.35 * math.sin(t * (5 + 2 * i) + i * 2.1)
            ancho *= 1 + 1.4 * v * onda
            s.push_opacity(min(1.0, alfa * n * (1 + 0.5 * v)))
            s.push_blur(desenfoque * (0.6 + 0.4 * self.nivel) * (1 + 0.5 * v))
            for rot0, vueltas in CAPAS:
                self._anillo(s, caja, w, h, radio, ancho, rot0 + self.giro * vueltas * 360, 0.75 if rot0 else 1.0)
            s.pop()
            s.pop()

    def _anillo(self, s, caja, w, h, radio, ancho, rotacion, opacidad):
        """Degradado cónico recortado a un anillo pegado al borde."""
        s.push_opacity(opacidad)
        s.push_mask(Gsk.MaskMode.ALPHA)
        # GTK graba primero la máscara (el anillo) y luego el contenido (el degradado)
        borde = Gsk.RoundedRect()
        borde.init_from_rect(caja, radio)
        s.append_border(borde, [ancho] * 4, [Gdk.RGBA(0, 0, 0, 1)] * 4)
        s.pop()
        s.append_conic_gradient(caja, Graphene.Point().init(w / 2, h / 2), rotacion, self.paradas)
        s.pop()
        s.pop()


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.omarchy.voz-es.aura")
        self.aura = None

    def do_activate(self):
        try:
            reducir = bool(json.loads(AJUSTES.read_text()).get("aura_reducir_movimiento"))
        except (FileNotFoundError, json.JSONDecodeError):
            reducir = False
        win = Gtk.Window(application=self, decorated=False, can_focus=False, focusable=False)
        win.add_css_class("aura")
        css = Gtk.CssProvider()
        css.load_from_string("window.aura { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Capa.init_for_window(win)
        Capa.set_namespace(win, "voz-es-aura")
        Capa.set_layer(win, Capa.Layer.OVERLAY)
        Capa.set_keyboard_mode(win, Capa.KeyboardMode.NONE)
        Capa.set_exclusive_zone(win, -1)  # ignora las barras: cubre todo el monitor
        for borde in (Capa.Edge.TOP, Capa.Edge.BOTTOM, Capa.Edge.LEFT, Capa.Edge.RIGHT):
            Capa.set_anchor(win, borde, True)
        self.aura = Aura(reducir)
        win.set_child(self.aura)
        # Sin región de entrada: el ratón y los clics atraviesan la aura
        win.connect("realize", lambda w: w.get_surface().set_input_region(cairo.Region()))
        win.present()
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, self._apagar)

    def _apagar(self):
        if self.aura:
            self.aura.apagar(self.quit)
        else:
            self.quit()
        return GLib.SOURCE_CONTINUE  # un 2.º SIGTERM no hace nada


if __name__ == "__main__":
    App().run()
