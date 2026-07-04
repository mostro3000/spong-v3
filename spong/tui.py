"""SPONG terminal dashboard (TUI).

Vista interactiva en vivo, estilo navegador, que se lee directamente de la
base de datos de archivos (var/database) y de la config (etc/*.yaml) — la
misma fuente que la UI Flask. Pensada para correr en el servidor spong por SSH.

    spong top        # o: spong-tui

Navegación:
  ↑/↓ o j/k     mover selección          Enter/→   entrar al host / expandir grupo;
  ←/h           volver / cerrar historial           sobre un servicio: ver su historial
  g/G           ir arriba / abajo         Tab       cambiar de panel
  m             detalle del servicio      a         mostrar/ocultar acks
  H             historial (servicio sel. o host)    r  refrescar     q  salir
  r             refrescar ahora           q         salir

Replica las reglas de color de la web:
  - servicio reconocido (ack) que no está en verde  → azul
  - servicio en rojo/amarillo dentro de una ventana de silencio → clear
  - color del host/grupo = el peor de sus servicios (azul cuenta como verde)
"""

from __future__ import annotations

import curses
import os
import time
from dataclasses import dataclass, field

from . import config, database
from .models import worst_color

REFRESH_SECONDS = 10

# color de spong -> (glifo, nombre de par de color curses)
_CIRCLE = "●"          # ●
_COLOR_ORDER = ("red", "purple", "yellow", "blue", "green", "clear")

_CLIENT_PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "plugins", "client")


# ---------------------------------------------------------------------------
# Capa de datos (sin Flask) — replica la lógica de coloreo de la web
# ---------------------------------------------------------------------------

_client_services_cache: set[str] | None = None


def _client_service_names() -> set[str]:
    global _client_services_cache
    if _client_services_cache is not None:
        return _client_services_cache
    names = set(config.get_checks())
    try:
        for filename in os.listdir(_CLIENT_PLUGIN_DIR):
            if not filename.endswith(".py"):
                continue
            name = filename[:-3]
            if name == "__init__" or name.startswith(("_", ".")):
                continue
            names.add(name)
    except OSError:
        pass
    _client_services_cache = names
    return names


def _visible_service_names(host: str) -> set[str]:
    configured = {svc for svc, _ in config.host_services(host)}
    stored = set(database.load_all_services(host).keys())
    return configured | _client_service_names() | stored


def _effective_services(host: str) -> dict:
    """Servicios visibles del host con el color efectivo (ack/silencio) aplicado."""
    allowed = _visible_service_names(host)
    services = {
        name: svc for name, svc in database.load_all_services(host).items()
        if name in allowed
    }
    acks = database.load_acks(host)
    for name, svc in services.items():
        if svc.color not in ("green", "blue") and any(a.covers(name) for a in acks):
            svc.color = "blue"
        if svc.color in ("red", "yellow") and config.is_suppressed(host, name):
            svc.color = "clear"
    return services


def _host_color(services: dict) -> str:
    if not services:
        return "green"
    return worst_color(["green" if s.color == "blue" else s.color
                        for s in services.values()])


def _service_sort_key(host: str, services: dict):
    cfg_order = [s for s, _ in config.host_services(host)]

    def key(name: str):
        return (cfg_order.index(name) if name in cfg_order else len(cfg_order), name)
    return sorted(services.keys(), key=key)


@dataclass
class HostNode:
    name: str
    color: str
    services: dict
    red_count: int


@dataclass
class GroupNode:
    key: str
    name: str
    color: str = "green"
    hosts: list = field(default_factory=list)
    red_count: int = 0


@dataclass
class Snapshot:
    groups: list = field(default_factory=list)
    ts: float = 0.0
    error: str = ""


def build_snapshot() -> Snapshot:
    """Relee config + base de datos y arma el árbol grupos→hosts→servicios."""
    try:
        config.load_all()
    except Exception as exc:  # noqa: BLE001 — queremos mostrar el error, no crashear
        return Snapshot(error=f"No pude leer la config: {exc}", ts=time.time())

    groups_cfg = config.get_groups()
    snap = Snapshot(ts=time.time())
    grouped_hosts: set[str] = set()

    for gkey, gdata in groups_cfg.items():
        if not gdata.get("display", True):
            continue
        members = gdata.get("members", []) or []
        node = GroupNode(key=gkey, name=gdata.get("name", gkey))
        for host in members:
            grouped_hosts.add(host)
            try:
                services = _effective_services(host)
            except Exception:  # noqa: BLE001 — un host roto no debe tumbar la vista
                services = {}
            hc = _host_color(services)
            reds = sum(1 for s in services.values() if s.color == "red")
            node.hosts.append(HostNode(host, hc, services, reds))
        node.color = worst_color([h.color for h in node.hosts]) if node.hosts else "green"
        node.red_count = sum(h.red_count for h in node.hosts)
        snap.groups.append(node)

    # Hosts en hosts.yaml que no están en ningún grupo mostrado: no ocultarlos.
    ungrouped = [h for h in config.get_hosts() if h not in grouped_hosts]
    if ungrouped:
        node = GroupNode(key="__ungrouped__", name="(sin grupo)")
        for host in ungrouped:
            try:
                services = _effective_services(host)
            except Exception:  # noqa: BLE001
                services = {}
            hc = _host_color(services)
            reds = sum(1 for s in services.values() if s.color == "red")
            node.hosts.append(HostNode(host, hc, services, reds))
        node.color = worst_color([h.color for h in node.hosts]) if node.hosts else "green"
        node.red_count = sum(h.red_count for h in node.hosts)
        snap.groups.append(node)

    return snap


# ---------------------------------------------------------------------------
# UI curses
# ---------------------------------------------------------------------------

_PAIRS = {
    "red": 1, "yellow": 2, "green": 3, "blue": 4, "purple": 5, "clear": 6,
    "header": 7, "hint": 8,
}


def _init_colors() -> None:
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(_PAIRS["red"], curses.COLOR_RED, bg)
    curses.init_pair(_PAIRS["yellow"], curses.COLOR_YELLOW, bg)
    curses.init_pair(_PAIRS["green"], curses.COLOR_GREEN, bg)
    curses.init_pair(_PAIRS["blue"], curses.COLOR_CYAN, bg)
    curses.init_pair(_PAIRS["purple"], curses.COLOR_MAGENTA, bg)
    curses.init_pair(_PAIRS["clear"], curses.COLOR_WHITE, bg)
    curses.init_pair(_PAIRS["header"], curses.COLOR_BLACK, curses.COLOR_BLUE)
    curses.init_pair(_PAIRS["hint"], curses.COLOR_BLACK, curses.COLOR_WHITE)


def _color_attr(color: str) -> int:
    pair = _PAIRS.get(color, _PAIRS["clear"])
    attr = curses.color_pair(pair)
    if color in ("red", "purple"):
        attr |= curses.A_BOLD
    if color == "clear":
        attr |= curses.A_DIM
    return attr


class TUI:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.snap = build_snapshot()
        self.expanded: set[str] = {g.key for g in self.snap.groups}  # todo expandido
        self.sel = 0                 # índice en la lista plana del panel izquierdo
        self.focus = "left"          # left | right
        self.svc_sel = 0             # servicio seleccionado en el panel derecho
        self.show_msg = False        # mostrar detalle (message) del servicio
        self.show_acks = False       # mostrar acks del host
        self.show_history = False    # mostrar historial
        self.history_service = None  # None = historial del host; str = de ese servicio
        self.last_refresh = time.time()
        self.status = ""

    # -- lista plana navegable del panel izquierdo -------------------------
    def _rows(self) -> list:
        """Devuelve filas (kind, group, host). kind: 'group' | 'host'."""
        rows = []
        for g in self.snap.groups:
            rows.append(("group", g, None))
            if g.key in self.expanded:
                for h in g.hosts:
                    rows.append(("host", g, h))
        return rows

    def _selected_host(self):
        rows = self._rows()
        if 0 <= self.sel < len(rows):
            kind, g, h = rows[self.sel]
            if kind == "host":
                return h
        return None

    def _selected_service_name(self):
        """Nombre del servicio seleccionado en el panel derecho, o None."""
        host = self._selected_host()
        if host is None:
            return None
        names = _service_sort_key(host.name, host.services)
        if names and 0 <= self.svc_sel < len(names):
            return names[self.svc_sel]
        return None

    # -- dibujo ------------------------------------------------------------
    def _add(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x - 1)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 6 or w < 40:
            self._add(0, 0, "Terminal muy chica (mín 40x6)")
            self.scr.refresh()
            return

        left_w = max(28, min(46, w // 2))

        # Cabecera
        elapsed = int(time.time() - self.last_refresh)
        countdown = max(0, REFRESH_SECONDS - elapsed)
        total_red = sum(g.red_count for g in self.snap.groups)
        title = " SPONG "
        clock = time.strftime("%H:%M:%S", time.localtime())
        header = f"{title}│ {total_red} en rojo │ actualiza en {countdown}s │ {clock}"
        self._add(0, 0, header.ljust(w - 1), curses.color_pair(_PAIRS["header"]) | curses.A_BOLD)

        # Paneles
        self._draw_left(1, 0, left_w, h - 2)
        self._draw_vsep(1, left_w, h - 2)
        self._draw_right(1, left_w + 2, w - left_w - 2, h - 2)

        # Pie de ayuda
        hint = " ↑↓ mover  ⏎ entrar/historial  ←/h volver  Tab panel  m detalle  a acks  H host/svc  r refrescar  q salir "
        self._add(h - 1, 0, hint.ljust(w - 1)[: w - 1], curses.color_pair(_PAIRS["hint"]))
        if self.status:
            self._add(h - 1, 0, f" {self.status} ", curses.color_pair(_PAIRS["hint"]) | curses.A_BOLD)

        self.scr.refresh()

    def _draw_vsep(self, y, x, height):
        for i in range(height):
            self._add(y + i, x, "│")

    def _draw_left(self, y0, x0, width, height):
        rows = self._rows()
        # scroll para mantener la selección visible
        top = 0
        if self.sel >= height:
            top = self.sel - height + 1
        visible = rows[top: top + height]
        for i, (kind, g, hnode) in enumerate(visible):
            idx = top + i
            y = y0 + i
            selected = (idx == self.sel)
            rowattr = curses.A_REVERSE if (selected and self.focus == "left") else 0
            if kind == "group":
                marker = "▾" if g.key in self.expanded else "▸"
                circle = _CIRCLE
                red = f"  {g.red_count}🔴" if g.red_count else ""
                # sin emoji para portabilidad: usamos círculo + conteo
                red = f"  ●{g.red_count}" if g.red_count else ""
                self._add(y, x0, f"{marker} ".ljust(2), rowattr)
                self._add(y, x0 + 2, circle, _color_attr(g.color) | rowattr)
                label = f" {g.name}"
                self._add(y, x0 + 3, label.ljust(width - 3 - len(red)), rowattr | curses.A_BOLD)
                if red:
                    self._add(y, x0 + width - len(red) - 1, red, _color_attr("red") | rowattr)
            else:
                circle = _CIRCLE
                self._add(y, x0 + 3, circle, _color_attr(hnode.color) | rowattr)
                label = f" {hnode.name}"
                self._add(y, x0 + 4, label.ljust(width - 4), rowattr)

    def _history_entries(self, host, svc_filter):
        """Transiciones de estado (7 días) del host, opcionalmente de un servicio."""
        try:
            entries = database.load_history(host.name, max_age_days=7, status_changes_only=True)
        except Exception:  # noqa: BLE001
            return None
        if svc_filter:
            entries = [e for e in entries if e.service == svc_filter]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def _draw_hist_rows(self, entries, x0, y, width, bottom, with_service):
        if entries is None:
            self._add(y, x0, "error leyendo el historial", _color_attr("red"))
            return
        if not entries:
            self._add(y, x0, "(sin eventos en el período)", curses.A_DIM)
            return
        for e in entries:
            if y >= bottom:
                self._add(y, x0, "…", curses.A_DIM)
                break
            ts = time.strftime("%d/%m %H:%M", time.localtime(e.timestamp))
            self._add(y, x0, ts, curses.A_DIM)
            self._add(y, x0 + 12, _CIRCLE, _color_attr(e.color or "clear"))
            if with_service:
                self._add(y, x0 + 14, f"{e.service:<12}", curses.A_BOLD)
                self._add(y, x0 + 27, (e.summary or "")[: width - 28])
            else:
                self._add(y, x0 + 14, (e.summary or e.color or "")[: width - 15])
            y += 1

    def _draw_right(self, y0, x0, width, height):
        host = self._selected_host()
        if host is None:
            self._add(y0, x0, "Elegí un host en el panel izquierdo (Tab / ⏎).", curses.A_DIM)
            return
        bottom = y0 + height - 1
        y = y0
        self._add(y, x0, host.name, curses.A_BOLD | _color_attr(host.color))
        y += 1
        self._add(y, x0, "─" * (width - 1), curses.A_DIM)
        y += 1

        # Vista ampliada de historial a pantalla completa (H / Enter sobre servicio)
        if self.show_history:
            self._draw_history(host, y0, x0, width, height, y)
            return

        # --- Servicios (estado actual) ---
        names = _service_sort_key(host.name, host.services)
        if not names:
            self._add(y, x0, "(sin servicios reportados)", curses.A_DIM)
        for i, name in enumerate(names):
            if y >= bottom:
                self._add(y, x0, "…", curses.A_DIM)
                break
            svc = host.services[name]
            selected = (self.focus == "right" and i == self.svc_sel)
            rowattr = curses.A_REVERSE if selected else 0
            self._add(y, x0, _CIRCLE, _color_attr(svc.color) | rowattr)
            self._add(y, x0 + 2, f"{name:<14}", rowattr | curses.A_BOLD)
            summ = svc.summary or ""
            self._add(y, x0 + 17, summ[: width - 18], rowattr)
            y += 1
            if self.show_msg and selected and svc.message:
                for mline in svc.message.splitlines()[:8]:
                    if y >= bottom:
                        break
                    self._add(y, x0 + 4, mline[: width - 5], curses.A_DIM)
                    y += 1

        if self.show_acks:
            y += 1
            self._add(y, x0, "Acks:", curses.A_BOLD)
            y += 1
            acks = database.load_acks(host.name)
            if not acks:
                self._add(y, x0 + 2, "(ninguno)", curses.A_DIM)
            for ack in acks:
                if y >= bottom:
                    break
                until = time.strftime("%d/%m %H:%M", time.localtime(ack.end_time)) if ack.end_time else "∞"
                self._add(y, x0 + 2, f"{ack.services} → {ack.contact} (hasta {until})"[: width - 3], curses.A_DIM)
                y += 1

        # --- Historial reciente inline (como la sección de la web) ---
        # Si estás parado sobre un servicio (panel derecho), se filtra a ese
        # servicio; si no, muestra los cambios de estado de todo el host.
        if y < bottom - 2:
            svc_filter = self._selected_service_name() if self.focus == "right" else None
            y += 1
            if svc_filter:
                self._add(y, x0, f"Historial de {svc_filter} (7d) — ⏎ o H para ampliar",
                          curses.A_BOLD)
            else:
                self._add(y, x0, "Historial del host (7d) — H para ampliar", curses.A_BOLD)
            y += 1
            self._draw_hist_rows(self._history_entries(host, svc_filter),
                                 x0, y, width, bottom, with_service=not svc_filter)

    def _draw_history(self, host, y0, x0, width, height, y):
        svc = self.history_service
        if svc:
            self._add(y, x0, f"Historial de {svc} (7 días) — cortes y regresos:", curses.A_BOLD)
        else:
            self._add(y, x0, "Historial del host (7 días) — cambios de estado:", curses.A_BOLD)
        y += 1
        self._add(y, x0, "H: alternar servicio/host    ← o h: volver", curses.A_DIM)
        y += 1
        self._draw_hist_rows(self._history_entries(host, svc),
                             x0, y, width, y0 + height - 1, with_service=not svc)

    # -- navegación --------------------------------------------------------
    def _move(self, delta):
        rows = self._rows()
        if not rows:
            return
        self.sel = max(0, min(len(rows) - 1, self.sel + delta))

    def _enter(self):
        # En el panel derecho, Enter sobre un servicio abre su historial
        # (cortes y regresos), como hacer clic en un servicio en la web.
        if self.focus == "right":
            svc = self._selected_service_name()
            if svc is not None:
                self.show_history = True
                self.history_service = svc
            return
        rows = self._rows()
        if not (0 <= self.sel < len(rows)):
            return
        kind, g, h = rows[self.sel]
        if kind == "group":
            if g.key in self.expanded:
                self.expanded.discard(g.key)
            else:
                self.expanded.add(g.key)
        else:
            self.focus = "right"
            self.svc_sel = 0

    def _back(self):
        # Si estamos viendo un historial, ← / h lo cierra primero.
        if self.show_history:
            self.show_history = False
            self.history_service = None
            return
        if self.focus == "right":
            self.focus = "left"
            return
        rows = self._rows()
        if 0 <= self.sel < len(rows):
            kind, g, h = rows[self.sel]
            if kind == "group" and g.key in self.expanded:
                self.expanded.discard(g.key)
            elif kind == "host":
                self.expanded.discard(g.key)
                # reposicionar la selección sobre la cabecera del grupo
                for i, (k2, g2, _h2) in enumerate(self._rows()):
                    if k2 == "group" and g2.key == g.key:
                        self.sel = i
                        break

    def _move_service(self, delta):
        host = self._selected_host()
        if host is None:
            return
        names = _service_sort_key(host.name, host.services)
        if not names:
            return
        self.svc_sel = max(0, min(len(names) - 1, self.svc_sel + delta))

    def refresh_data(self):
        self.snap = build_snapshot()
        # mantener expandidos los grupos que sigan existiendo; expandir nuevos
        keys = {g.key for g in self.snap.groups}
        self.expanded = {k for k in self.expanded if k in keys} or keys
        for g in self.snap.groups:
            self.expanded.add(g.key)
        rows = self._rows()
        self.sel = min(self.sel, max(0, len(rows) - 1))
        self.last_refresh = time.time()

    # -- loop principal ----------------------------------------------------
    def run(self):
        curses.curs_set(0)
        self.scr.timeout(1000)   # getch cada 1s → refresca el countdown
        while True:
            self._draw()
            try:
                ch = self.scr.getch()
            except KeyboardInterrupt:
                break

            if ch == -1:
                if time.time() - self.last_refresh >= REFRESH_SECONDS:
                    self.refresh_data()
                continue

            self.status = ""
            if ch in (ord("q"), ord("Q")):
                break
            elif ch in (curses.KEY_DOWN, ord("j")):
                self._move(1) if self.focus == "left" else self._move_service(1)
            elif ch in (curses.KEY_UP, ord("k")):
                self._move(-1) if self.focus == "left" else self._move_service(-1)
            elif ch in (curses.KEY_RIGHT, ord("l"), ord("\n"), curses.KEY_ENTER, 10, 13):
                self._enter()
            elif ch in (curses.KEY_LEFT, ord("h")):
                self._back()
            elif ch == ord("\t"):
                self.focus = "right" if self.focus == "left" and self._selected_host() else "left"
            elif ch == ord("g"):
                self.sel = 0
            elif ch == ord("G"):
                self.sel = max(0, len(self._rows()) - 1)
            elif ch in (ord("m"), ord("M")):
                self.show_msg = not self.show_msg
            elif ch in (ord("a"), ord("A")):
                self.show_acks = not self.show_acks
            elif ch == ord("H"):
                # H: entra al historial. Si estás sobre un servicio (panel
                # derecho), filtra a ese servicio (cortes/regresos); si no, el
                # del host. Repetir alterna servicio→host→apagar.
                if not self.show_history:
                    self.show_history = True
                    self.history_service = (
                        self._selected_service_name() if self.focus == "right" else None
                    )
                elif self.history_service is not None:
                    self.history_service = None
                else:
                    self.show_history = False
            elif ch in (ord("r"), ord("R")):
                self.refresh_data()
                self.status = "actualizado"
            elif ch == curses.KEY_RESIZE:
                pass


def _run(stdscr):
    _init_colors()
    TUI(stdscr).run()


def main():
    config.load_all()
    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
