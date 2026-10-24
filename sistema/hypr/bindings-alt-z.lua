-- Asistente de voz local (~/voz-es): Alt+Z abre la ventanita y conversa; otra vez Alt+Z la cierra.
-- Antes abría la terminal de Claude Code (~/.local/bin/asistente).
o.bind("ALT + Z", "Asistente de voz (pulsa para hablar, otra vez para cerrar)", os.getenv("HOME") .. "/.local/bin/voz-es-ctl down")
