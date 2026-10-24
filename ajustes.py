#!/usr/bin/env python3
"""Ajustes del asistente de voz (Alt+Z): dónde sale la ventanita, su tamaño, opacidad y la voz.

Usa el Python del sistema (GTK4 + libadwaita). Guarda en ~/.config/voz-es/ajustes.json y genera
~/.config/hypr/voz_es.lua con la regla de la ventanita; el asistente lee la voz al hablar.
"""

import json
import subprocess
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

AJUSTES = Path.home() / ".config/voz-es/ajustes.json"
REGLA = Path.home() / ".config/hypr/voz_es.lua"
PANEL = "org.omarchy.voz-es"

POR_DEFECTO = {"posicion": "abajo-derecha", "margen": 40, "ancho": 440, "alto": 190,
               "opacidad": 0.88, "fijar": True, "voz": "ef_dora", "velocidad": 1.0}
POSICIONES = [("abajo-derecha", "Abajo a la derecha"), ("abajo-izquierda", "Abajo a la izquierda"),
              ("arriba-derecha", "Arriba a la derecha"), ("arriba-izquierda", "Arriba a la izquierda"),
              ("abajo-centro", "Abajo en el centro")]
VOCES = [("ef_dora", "Dora (mujer)"), ("em_alex", "Alex (hombre)"), ("em_santa", "Santa (hombre, grave)")]


def leer() -> dict:
    try:
        return POR_DEFECTO | json.loads(AJUSTES.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(POR_DEFECTO)


def regla_lua(a: dict) -> str:
    m, w, h = int(a["margen"]), int(a["ancho"]), int(a["alto"])
    vertical, horizontal = a["posicion"].split("-")
    # Con el tamaño en número: window_w/h aún valen el tamaño inicial cuando se aplica «move»
    x = {"izquierda": f'"{m}"', "derecha": f'"(monitor_w-{w + m})"', "centro": f'"((monitor_w-{w})/2)"'}[horizontal]
    y = {"arriba": f'"{m}"', "abajo": f'"(monitor_h-{h + m})"'}[vertical]
    op = f'{a["opacidad"]:.2f}'
    return f"""-- Generado por «Asistente de voz · Ajustes» (~/voz-es/ajustes.py). No lo edites a mano.
o.window("^org\\\\.omarchy\\\\.voz-es$", {{
  tag = "-default-opacity",
  float = true,
  pin = {str(bool(a["fijar"])).lower()},
  no_initial_focus = true,
  size = {{ {int(a["ancho"])}, {int(a["alto"])} }},
  move = {{ {x}, {y} }},
  opacity = "{op} {op}",
}})

-- La propia app de ajustes: flotante y centrada
o.window("^org\\\\.omarchy\\\\.voz-es\\\\.ajustes$", {{ float = true, center = true, size = {{ 520, 760 }} }})
"""


def guardar(a: dict):
    AJUSTES.parent.mkdir(parents=True, exist_ok=True)
    AJUSTES.write_text(json.dumps(a, indent=2, ensure_ascii=False) + "\n")
    REGLA.write_text(regla_lua(a))
    subprocess.run(["hyprctl", "reload"], capture_output=True)


def panel_abierto() -> bool:
    out = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True).stdout
    try:
        return any(c.get("class") == PANEL for c in json.loads(out))
    except json.JSONDecodeError:
        return False


def abrir_panel():
    subprocess.Popen(["omarchy-launch-tui", f"--app-id={PANEL}", str(Path.home() / ".local/bin/voz-es-panel")],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cerrar_panel():
    subprocess.run(["pkill", "-f", f"app-id={PANEL}"])


class Ventana(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Asistente de voz", default_width=460, default_height=640)
        self.a = leer()
        self.vista_previa = None

        pagina = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Ventanita", description="Aparece al pulsar Alt+Z. "
                                 "Para moverla a mano: Super + arrastrar con el ratón.")
        self.posicion = Adw.ComboRow(title="Posición", model=Gtk.StringList.new([t for _, t in POSICIONES]))
        self.posicion.set_selected([k for k, _ in POSICIONES].index(self.a["posicion"]))
        g.add(self.posicion)
        self.margen = self._numero(g, "Separación del borde", "píxeles", self.a["margen"], 0, 400, 5)
        self.ancho = self._numero(g, "Ancho", "píxeles", self.a["ancho"], 200, 1600, 10)
        self.alto = self._numero(g, "Alto", "píxeles", self.a["alto"], 80, 1200, 10)

        fila = Adw.ActionRow(title="Opacidad")
        self.opacidad = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.3, 1.0, 0.01)
        self.opacidad.set_value(self.a["opacidad"])
        self.opacidad.set_size_request(200, -1)
        self.opacidad.set_draw_value(True)
        self.opacidad.set_digits(2)
        fila.add_suffix(self.opacidad)
        g.add(fila)

        self.fijar = Adw.SwitchRow(title="Visible en todos los escritorios", active=self.a["fijar"])
        g.add(self.fijar)
        pagina.add(g)

        g = Adw.PreferencesGroup(title="Voz")
        self.voz = Adw.ComboRow(title="Voz", model=Gtk.StringList.new([t for _, t in VOCES]))
        claves = [k for k, _ in VOCES]
        self.voz.set_selected(claves.index(self.a["voz"]) if self.a["voz"] in claves else 0)
        g.add(self.voz)
        self.velocidad = self._numero(g, "Velocidad al hablar", "1 = normal", self.a["velocidad"], 0.6, 1.6, 0.05, 2)
        pagina.add(g)

        g = Adw.PreferencesGroup()
        caja = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6)
        probar = Gtk.Button(label="Vista previa")
        probar.connect("clicked", self._probar)
        guardar_b = Gtk.Button(label="Guardar", css_classes=["suggested-action"])
        guardar_b.connect("clicked", self._guardar)
        restaurar = Gtk.Button(label="Valores por defecto")
        restaurar.connect("clicked", self._restaurar)
        for b in (restaurar, probar, guardar_b):
            caja.append(b)
        g.add(caja)
        pagina.add(g)

        self.toasts = Adw.ToastOverlay(child=pagina)
        vista = Adw.ToolbarView(content=self.toasts)
        vista.add_top_bar(Adw.HeaderBar())
        self.set_content(vista)

    def _numero(self, grupo, titulo, sub, valor, mini, maxi, paso, decimales=0):
        fila = Adw.SpinRow.new_with_range(mini, maxi, paso)
        fila.set_title(titulo)
        fila.set_subtitle(sub)
        fila.set_digits(decimales)
        fila.set_value(valor)
        grupo.add(fila)
        return fila

    def _recoger(self) -> dict:
        return {"posicion": POSICIONES[self.posicion.get_selected()][0],
                "margen": int(self.margen.get_value()), "ancho": int(self.ancho.get_value()),
                "alto": int(self.alto.get_value()), "opacidad": round(self.opacidad.get_value(), 2),
                "fijar": self.fijar.get_active(), "voz": VOCES[self.voz.get_selected()][0],
                "velocidad": round(self.velocidad.get_value(), 2)}

    def _aplicar(self):
        """Guarda y, si la ventanita está abierta, la reabre para que coja los cambios."""
        self.a = leer() | self._recoger()  # conserva claves que la app no muestra (p. ej. «nombre»)
        guardar(self.a)
        if panel_abierto():
            cerrar_panel()
            GLib.timeout_add(300, lambda: abrir_panel() or False)

    def _guardar(self, _b):
        self._aplicar()
        self.toasts.add_toast(Adw.Toast(title="Guardado. La voz nueva se usa en la próxima respuesta.", timeout=3))

    def _probar(self, _b):
        self._aplicar()
        if not panel_abierto():
            abrir_panel()
        if self.vista_previa:
            GLib.source_remove(self.vista_previa)
        self.vista_previa = GLib.timeout_add_seconds(6, self._fin_vista_previa)

    def _fin_vista_previa(self):
        self.vista_previa = None
        cerrar_panel()
        return False

    def _restaurar(self, _b):
        a = POR_DEFECTO
        self.posicion.set_selected([k for k, _ in POSICIONES].index(a["posicion"]))
        for w, k in ((self.margen, "margen"), (self.ancho, "ancho"), (self.alto, "alto"),
                     (self.opacidad, "opacidad"), (self.velocidad, "velocidad")):
            w.set_value(a[k])
        self.fijar.set_active(a["fijar"])
        self.voz.set_selected(0)


def main():
    if not REGLA.exists():
        guardar(leer())
    app = Adw.Application(application_id="org.omarchy.voz-es.ajustes")
    app.connect("activate", lambda app: Ventana(app).present())
    app.run()


if __name__ == "__main__":
    main()
