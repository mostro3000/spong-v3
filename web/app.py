#!/usr/bin/env python3
"""SPONG web interface - Flask application.

Run with:
    python3 /usr/local/spong/web/app.py
    # or via gunicorn:
    gunicorn -w 4 -b 0.0.0.0:8080 app:app
"""

import sys
import time
import os

sys.path.insert(0, "/usr/local/spong")

try:
    from flask import Flask, render_template, redirect, url_for, request, jsonify, Response
except ImportError:
    print("Flask not installed. Run: pip3 install flask", file=sys.stderr)
    sys.exit(1)

from spong import config, database
from spong.models import worst_color

_COLOR_ORDER = {"red": 0, "yellow": 1, "purple": 2, "blue": 3, "clear": 4, "green": 5}
from spong.status_sender import send_ack, send_ack_del

config.load_all()

app = Flask(__name__, template_folder="templates")

# Support reverse-proxy with path prefix (e.g. Apache ProxyPass /spong → localhost:8090)
# Apache must set:  RequestHeader set X-Forwarded-Prefix /spong
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1, x_prefix=1)


def _check_auth(username, password):
    expected_user = config.get("web.auth_user", "")
    expected_pass = config.get("web.auth_password", "")
    if not expected_user:
        return True
    return username == expected_user and password == expected_pass


@app.before_request
def require_auth():
    expected_user = config.get("web.auth_user", "")
    if not expected_user:
        return  # auth deshabilitada
    auth = request.authorization
    if not auth or not _check_auth(auth.username, auth.password):
        return Response(
            "Acceso restringido - ingresá usuario y contraseña.",
            401,
            {"WWW-Authenticate": 'Basic realm="SPONG"'},
        )

# ---- Color helpers ----
COLORS_CSS = {
    "red": "#cc0000",
    "yellow": "#ffff00",
    "green": "#339900",
    "purple": "#990099",
    "blue": "#0000ff",
    "clear": "#ffffff",
}


def color_style(color: str) -> str:
    return f"background-color:{COLORS_CSS.get(color, '#ffffff')};color:{'#fff' if color in ('red','purple','blue') else '#000'}"


app.jinja_env.globals["color_style"] = color_style
app.jinja_env.globals["worst_color"] = worst_color
app.jinja_env.globals["time"] = time

# ---------------------------------------------------------------------------
# i18n — simple dict-based translation, no external deps
# ---------------------------------------------------------------------------
_SUPPORTED_LANGS = ("es", "en", "fr", "de", "pt", "zh", "ru")

_LANG_META = {
    "es": ("🇪🇸", "Español"),
    "en": ("🇬🇧", "English"),
    "fr": ("🇫🇷", "Français"),
    "de": ("🇩🇪", "Deutsch"),
    "pt": ("🇧🇷", "Português"),
    "zh": ("🇨🇳", "中文"),
    "ru": ("🇷🇺", "Русский"),
}

_TRANSLATIONS: dict = {
    "en": {
        "Grupos": "Groups", "Problemas": "Problems", "Reconocidos": "Acknowledged",
        "Con problemas": "With problems", "Sin problemas": "No problems", "grupos": "groups",
        "Actualizar ahora": "Refresh now", "clic para cerrar": "click to close",
        "SPONG v3.1 — creado por mt": "SPONG v3.1 — created by mt",
        "Grupos de Hosts": "Host Groups", "Sin datos.": "No data.",
        "Servicio": "Service", "Servicios": "Services", "Estado": "Status",
        "Resumen": "Summary", "Contacto": "Contact", "Hasta": "Until",
        "Mensaje": "Message", "Borrar": "Delete", "Reconocer": "Acknowledge",
        "En este estado": "In this state", "Último reporte": "Last report",
        "Graf.": "Graph", "Sin vencimiento": "Never", "desde": "since",
        "Reconocimientos activos": "Active acknowledgements",
        "Historial (últimos 7 días)": "History (last 7 days)",
        "Fecha": "Date", "Tipo": "Type",
        "Problemas actuales": "Current problems",
        "Todo OK — sin problemas activos": "All OK — no active problems",
        "Detalles del servicio": "Service details", "Última actualización": "Last update",
        "Reconocido": "Acknowledged", "Detalle": "Detail", "Gráficos": "Graphs",
        "1 hora": "1 hour", "24 horas": "24 hours", "7 días": "7 days",
        "30 días": "30 days", "1 año": "1 year",
        "No hay datos para este servicio.": "No data for this service.",
        "No hay reconocimientos activos.": "No active acknowledgements.",
        "Patrón": "Pattern", "Estado actual": "Current status",
        "restantes": "remaining", "+ Nuevo reconocimiento": "+ New acknowledgement",
        "Nuevo reconocimiento": "New acknowledgement", "Duración": "Duration", "Cancelar": "Cancel",
    },
    "fr": {
        "Grupos": "Groupes", "Problemas": "Problèmes", "Reconocidos": "Acquittements",
        "Con problemas": "Avec problèmes", "Sin problemas": "Sans problèmes", "grupos": "groupes",
        "Actualizar ahora": "Actualiser", "clic para cerrar": "cliquer pour fermer",
        "SPONG v3.1 — creado por mt": "SPONG v3.1 — créé par mt",
        "Grupos de Hosts": "Groupes d'hôtes", "Sin datos.": "Pas de données.",
        "Servicio": "Service", "Servicios": "Services", "Estado": "État",
        "Resumen": "Résumé", "Contacto": "Contact", "Hasta": "Jusqu'à",
        "Mensaje": "Message", "Borrar": "Supprimer", "Reconocer": "Acquitter",
        "En este estado": "Dans cet état", "Último reporte": "Dernier rapport",
        "Graf.": "Graph", "Sin vencimiento": "Jamais", "desde": "depuis",
        "Reconocimientos activos": "Acquittements actifs",
        "Historial (últimos 7 días)": "Historique (7 derniers jours)",
        "Fecha": "Date", "Tipo": "Type",
        "Problemas actuales": "Problèmes actuels",
        "Todo OK — sin problemas activos": "Tout OK — aucun problème actif",
        "Detalles del servicio": "Détails du service", "Última actualización": "Dernière mise à jour",
        "Reconocido": "Acquitté", "Detalle": "Détail", "Gráficos": "Graphiques",
        "1 hora": "1 heure", "24 horas": "24 heures", "7 días": "7 jours",
        "30 días": "30 jours", "1 año": "1 an",
        "No hay datos para este servicio.": "Pas de données pour ce service.",
        "No hay reconocimientos activos.": "Aucun acquittement actif.",
        "Patrón": "Modèle", "Estado actual": "État actuel",
        "restantes": "restants", "+ Nuevo reconocimiento": "+ Nouvel acquittement",
        "Nuevo reconocimiento": "Nouvel acquittement", "Duración": "Durée", "Cancelar": "Annuler",
    },
    "de": {
        "Grupos": "Gruppen", "Problemas": "Probleme", "Reconocidos": "Bestätigt",
        "Con problemas": "Mit Problemen", "Sin problemas": "Keine Probleme", "grupos": "Gruppen",
        "Actualizar ahora": "Jetzt aktualisieren", "clic para cerrar": "klicken zum Schließen",
        "SPONG v3.1 — creado por mt": "SPONG v3.1 — erstellt von mt",
        "Grupos de Hosts": "Host-Gruppen", "Sin datos.": "Keine Daten.",
        "Servicio": "Dienst", "Servicios": "Dienste", "Estado": "Status",
        "Resumen": "Zusammenfassung", "Contacto": "Kontakt", "Hasta": "Bis",
        "Mensaje": "Nachricht", "Borrar": "Löschen", "Reconocer": "Bestätigen",
        "En este estado": "In diesem Zustand", "Último reporte": "Letzter Bericht",
        "Graf.": "Graph", "Sin vencimiento": "Nie", "desde": "seit",
        "Reconocimientos activos": "Aktive Bestätigungen",
        "Historial (últimos 7 días)": "Verlauf (letzte 7 Tage)",
        "Fecha": "Datum", "Tipo": "Typ",
        "Problemas actuales": "Aktuelle Probleme",
        "Todo OK — sin problemas activos": "Alles OK — keine aktiven Probleme",
        "Detalles del servicio": "Dienstdetails", "Última actualización": "Letzte Aktualisierung",
        "Reconocido": "Bestätigt", "Detalle": "Detail", "Gráficos": "Graphen",
        "1 hora": "1 Stunde", "24 horas": "24 Stunden", "7 días": "7 Tage",
        "30 días": "30 Tage", "1 año": "1 Jahr",
        "No hay datos para este servicio.": "Keine Daten für diesen Dienst.",
        "No hay reconocimientos activos.": "Keine aktiven Bestätigungen.",
        "Patrón": "Muster", "Estado actual": "Aktueller Status",
        "restantes": "verbleibend", "+ Nuevo reconocimiento": "+ Neue Bestätigung",
        "Nuevo reconocimiento": "Neue Bestätigung", "Duración": "Dauer", "Cancelar": "Abbrechen",
    },
    "pt": {
        "Grupos": "Grupos", "Problemas": "Problemas", "Reconocidos": "Reconhecidos",
        "Con problemas": "Com problemas", "Sin problemas": "Sem problemas", "grupos": "grupos",
        "Actualizar ahora": "Atualizar agora", "clic para cerrar": "clique para fechar",
        "SPONG v3.1 — creado por mt": "SPONG v3.1 — criado por mt",
        "Grupos de Hosts": "Grupos de Hosts", "Sin datos.": "Sem dados.",
        "Servicio": "Serviço", "Servicios": "Serviços", "Estado": "Estado",
        "Resumen": "Resumo", "Contacto": "Contato", "Hasta": "Até",
        "Mensaje": "Mensagem", "Borrar": "Excluir", "Reconocer": "Reconhecer",
        "En este estado": "Neste estado", "Último reporte": "Último relatório",
        "Graf.": "Gráfico", "Sin vencimiento": "Nunca", "desde": "desde",
        "Reconocimientos activos": "Reconhecimentos ativos",
        "Historial (últimos 7 días)": "Histórico (últimos 7 dias)",
        "Fecha": "Data", "Tipo": "Tipo",
        "Problemas actuales": "Problemas atuais",
        "Todo OK — sin problemas activos": "Tudo OK — sem problemas ativos",
        "Detalles del servicio": "Detalhes do serviço", "Última actualización": "Última atualização",
        "Reconocido": "Reconhecido", "Detalle": "Detalhe", "Gráficos": "Gráficos",
        "1 hora": "1 hora", "24 horas": "24 horas", "7 días": "7 dias",
        "30 días": "30 dias", "1 año": "1 ano",
        "No hay datos para este servicio.": "Sem dados para este serviço.",
        "No hay reconocimientos activos.": "Nenhum reconhecimento ativo.",
        "Patrón": "Padrão", "Estado actual": "Estado atual",
        "restantes": "restantes", "+ Nuevo reconocimiento": "+ Novo reconhecimento",
        "Nuevo reconocimiento": "Novo reconhecimento", "Duración": "Duração", "Cancelar": "Cancelar",
    },
    "zh": {
        "Grupos": "组", "Problemas": "问题", "Reconocidos": "已确认",
        "Con problemas": "有问题", "Sin problemas": "无问题", "grupos": "组",
        "Actualizar ahora": "立即刷新", "clic para cerrar": "点击关闭",
        "SPONG v3.1 — creado por mt": "SPONG v3.1 — 由 mt 创建",
        "Grupos de Hosts": "主机组", "Sin datos.": "无数据。",
        "Servicio": "服务", "Servicios": "服务", "Estado": "状态",
        "Resumen": "摘要", "Contacto": "联系人", "Hasta": "截止",
        "Mensaje": "消息", "Borrar": "删除", "Reconocer": "确认",
        "En este estado": "在此状态", "Último reporte": "最后报告",
        "Graf.": "图表", "Sin vencimiento": "永不", "desde": "自",
        "Reconocimientos activos": "活动确认",
        "Historial (últimos 7 días)": "历史（最近7天）",
        "Fecha": "日期", "Tipo": "类型",
        "Problemas actuales": "当前问题",
        "Todo OK — sin problemas activos": "一切正常——无活动问题",
        "Detalles del servicio": "服务详情", "Última actualización": "最后更新",
        "Reconocido": "已确认", "Detalle": "详情", "Gráficos": "图表",
        "1 hora": "1小时", "24 horas": "24小时", "7 días": "7天",
        "30 días": "30天", "1 año": "1年",
        "No hay datos para este servicio.": "此服务无数据。",
        "No hay reconocimientos activos.": "无活动确认。",
        "Patrón": "模式", "Estado actual": "当前状态",
        "restantes": "剩余", "+ Nuevo reconocimiento": "+ 新确认",
        "Nuevo reconocimiento": "新确认", "Duración": "持续时间", "Cancelar": "取消",
    },
    "ru": {
        "Grupos": "Группы", "Problemas": "Проблемы", "Reconocidos": "Подтверждено",
        "Con problemas": "С проблемами", "Sin problemas": "Нет проблем", "grupos": "групп",
        "Actualizar ahora": "Обновить", "clic para cerrar": "нажмите для закрытия",
        "SPONG v3.1 — creado por mt": "SPONG v3.1 — создано mt",
        "Grupos de Hosts": "Группы хостов", "Sin datos.": "Нет данных.",
        "Servicio": "Сервис", "Servicios": "Сервисы", "Estado": "Статус",
        "Resumen": "Сводка", "Contacto": "Контакт", "Hasta": "До",
        "Mensaje": "Сообщение", "Borrar": "Удалить", "Reconocer": "Подтвердить",
        "En este estado": "В этом состоянии", "Último reporte": "Последний отчёт",
        "Graf.": "График", "Sin vencimiento": "Никогда", "desde": "с",
        "Reconocimientos activos": "Активные подтверждения",
        "Historial (últimos 7 días)": "История (последние 7 дней)",
        "Fecha": "Дата", "Tipo": "Тип",
        "Problemas actuales": "Текущие проблемы",
        "Todo OK — sin problemas activos": "Всё OK — нет активных проблем",
        "Detalles del servicio": "Детали сервиса", "Última actualización": "Последнее обновление",
        "Reconocido": "Подтверждено", "Detalle": "Детали", "Gráficos": "Графики",
        "1 hora": "1 час", "24 horas": "24 часа", "7 días": "7 дней",
        "30 días": "30 дней", "1 año": "1 год",
        "No hay datos para este servicio.": "Нет данных для этого сервиса.",
        "No hay reconocimientos activos.": "Нет активных подтверждений.",
        "Patrón": "Шаблон", "Estado actual": "Текущий статус",
        "restantes": "осталось", "+ Nuevo reconocimiento": "+ Новое подтверждение",
        "Nuevo reconocimiento": "Новое подтверждение", "Duración": "Длительность", "Cancelar": "Отмена",
    },
}


def _make_translator(lang: str):
    d = _TRANSLATIONS.get(lang, {})
    def t(s: str) -> str:
        return d.get(s, s)
    return t


def _make_time_ago(lang: str):
    def f(seconds: int) -> str:
        seconds = int(seconds)
        if lang == "en":
            if seconds < 60:    return f"{seconds}s ago"
            if seconds < 3600:  return f"{seconds // 60}m ago"
            if seconds < 86400: return f"{seconds // 3600}h ago"
            return f"{seconds // 86400}d ago"
        elif lang == "fr":
            if seconds < 60:    return f"il y a {seconds}s"
            if seconds < 3600:  return f"il y a {seconds // 60}m"
            if seconds < 86400: return f"il y a {seconds // 3600}h"
            return f"il y a {seconds // 86400}j"
        elif lang == "de":
            if seconds < 60:    return f"vor {seconds}s"
            if seconds < 3600:  return f"vor {seconds // 60}m"
            if seconds < 86400: return f"vor {seconds // 3600}h"
            return f"vor {seconds // 86400}T"
        elif lang == "pt":
            if seconds < 60:    return f"há {seconds}s"
            if seconds < 3600:  return f"há {seconds // 60}m"
            if seconds < 86400: return f"há {seconds // 3600}h"
            return f"há {seconds // 86400}d"
        elif lang == "zh":
            if seconds < 60:    return f"{seconds}秒前"
            if seconds < 3600:  return f"{seconds // 60}分钟前"
            if seconds < 86400: return f"{seconds // 3600}小时前"
            return f"{seconds // 86400}天前"
        elif lang == "ru":
            if seconds < 60:    return f"{seconds}с назад"
            if seconds < 3600:  return f"{seconds // 60}м назад"
            if seconds < 86400: return f"{seconds // 3600}ч назад"
            return f"{seconds // 86400}д назад"
        else:  # es
            if seconds < 60:    return f"hace {seconds}s"
            if seconds < 3600:  return f"hace {seconds // 60}m"
            if seconds < 86400: return f"hace {seconds // 3600}h"
            return f"hace {seconds // 86400}d"
    return f


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:    return f"{seconds}s"
    if seconds < 3600:  return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400: return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


app.jinja_env.globals["fmt_duration"] = _fmt_duration


@app.context_processor
def inject_i18n():
    lang = request.cookies.get("lang", "es")
    if lang not in _SUPPORTED_LANGS:
        lang = "es"
    theme = request.cookies.get("theme", "light")
    if theme not in ("light", "dark"):
        theme = "light"
    return {
        "_": _make_translator(lang),
        "current_lang": lang,
        "time_ago": _make_time_ago(lang),
        "lang_meta": _LANG_META,
        "current_theme": theme,
    }


@app.template_filter("strftime")
def _strftime(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        return time.strftime(fmt, time.localtime(float(ts)))
    except Exception:
        return "?"


@app.context_processor
def inject_sidebar():
    groups = config.get_groups()
    sidebar = []
    for gname, gdata in groups.items():
        if not gdata.get("display", True):
            continue
        members = gdata.get("members", [])
        red_items = []
        for host in members:
            svcs = database.load_all_services(host)
            acks = database.load_acks(host)
            _apply_ack_colors(svcs, acks)
            red_svcs = [svc_name for svc_name, svc in svcs.items() if svc.color == "red"]
            if len(red_svcs) == 1:
                red_items.append({"host": host, "service": red_svcs[0]})
            elif len(red_svcs) > 1:
                red_items.append({"host": host, "service": "multiple"})
        if not red_items:
            continue
        sidebar.append({
            "name": gdata.get("name", gname),
            "key": gname,
            "problems": red_items,
            "count": len(red_items),
        })
    return {"sidebar_groups": sidebar}


# ---- Routes ----

@app.route("/")
def index():
    groups = config.get_groups()
    group_data = []
    for gname, gdata in groups.items():
        if not gdata.get("display", True):
            continue
        members = gdata.get("members", [])
        host_statuses = {}
        for host in members:
            services = database.load_all_services(host)
            acks = database.load_acks(host)
            _apply_ack_colors(services, acks)
            host_color = worst_color([
                "green" if s.color == "blue" else s.color
                for s in services.values()
            ]) if services else "green"
            host_statuses[host] = {"color": host_color, "services": services}
        # Blue (acked) doesn't escalate group color — treat it as green
        group_color = worst_color([
            "green" if v["color"] == "blue" else v["color"]
            for v in host_statuses.values()
        ]) if host_statuses else "green"
        # Build service column order from hosts.yaml order (ping always first)
        seen_cols = {}
        for host in members:
            for svc_name, _ in config.host_services(host):
                if svc_name not in seen_cols:
                    seen_cols[svc_name] = None
        # Also include any services present in DB but not in config
        for data in host_statuses.values():
            for svc_name in data["services"]:
                if svc_name not in seen_cols:
                    seen_cols[svc_name] = None
        # Ensure paired services are adjacent: https after http, telnet after ssh
        _pairs = {"http": "https", "ssh": "telnet"}
        cols = list(seen_cols.keys())
        service_cols = []
        skip = set()
        for svc in cols:
            if svc in skip:
                continue
            service_cols.append(svc)
            partner = _pairs.get(svc)
            if partner and partner in seen_cols:
                service_cols.append(partner)
                skip.add(partner)
        group_data.append({
            "name": gdata.get("name", gname),
            "key": gname,
            "color": group_color,
            "hosts": host_statuses,
            "compress": gdata.get("compress", False),
            "service_cols": service_cols,
        })
    return render_template("index.html", groups=group_data)


def _apply_ack_colors(services: dict, acks: list) -> None:
    """Mark acked non-green services as blue, in-place."""
    for svc_name, svc in services.items():
        if svc.color not in ("green", "clear", "blue"):
            if any(ack.covers(svc_name) for ack in acks):
                svc.color = "blue"


@app.route("/host/<hostname>")
def host_detail(hostname):
    host_cfg = config.get_host(hostname)
    services = database.load_all_services(hostname)
    acks = database.load_acks(hostname)
    _apply_ack_colors(services, acks)
    history = database.load_history(hostname, max_age_days=7)
    # Order services by hosts.yaml order, then any extras alphabetically
    cfg_order = [s for s, _ in config.host_services(hostname)]
    sorted_services = sorted(
        services.items(),
        key=lambda kv: (cfg_order.index(kv[0]) if kv[0] in cfg_order else len(cfg_order), kv[0])
    )
    return render_template(
        "host.html",
        hostname=hostname,
        host_cfg=host_cfg,
        services=sorted_services,
        acks=acks,
        history=sorted(history, key=lambda e: e.timestamp, reverse=True),
    )


@app.route("/service/<hostname>/<service>")
def service_detail(hostname, service):
    svc = database.load_service(hostname, service)
    acks = database.load_acks(hostname)
    is_acked = database.is_acknowledged(hostname, service)
    return render_template(
        "service.html",
        hostname=hostname,
        service=service,
        svc=svc,
        acks=acks,
        is_acked=is_acked,
    )


@app.route("/problems")
def problems():
    hosts = config.get_hosts()
    issues = []
    for hostname in hosts:
        services = database.load_all_services(hostname)
        acks = database.load_acks(hostname)
        _apply_ack_colors(services, acks)
        for svc_name, svc in sorted(services.items()):
            if svc.color in ("red", "yellow", "purple"):
                issues.append({
                    "host": hostname,
                    "service": svc_name,
                    "color": svc.color,
                    "summary": svc.summary,
                    "duration": svc.duration,
                    "start_time": svc.start_time,
                    "report_time": svc.report_time,
                    "lag": time.time() - svc.report_time,
                })
    issues.sort(key=lambda x: (x["color"] != "red", x["color"] != "yellow"))
    return render_template("problems.html", issues=issues)


@app.route("/acks")
def acks_page():
    hosts = config.get_hosts()
    all_acks = []
    for hostname in hosts:
        svcs = database.load_all_services(hostname)
        for ack in database.load_acks(hostname):
            services_status = [(sn, sv) for sn, sv in svcs.items() if ack.covers(sn)]
            all_acks.append({
                "ack": ack,
                "services_status": services_status,
            })
    all_acks.sort(key=lambda x: x["ack"].end_time)
    return render_template("acks.html", all_acks=all_acks)


def _parse_duration(value: str) -> float:
    """Parse duration like +4h, +2d, +1m, +1a, never into seconds. Returns 0 for never."""
    import re
    value = value.strip().lstrip("+")
    if value.lower() in ("never", "siempre", "0"):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hdmayHDMAY]?)", value)
    if not m:
        return 4 * 3600  # default 4 horas
    n = float(m.group(1))
    unit = m.group(2).lower()
    return n * {"h": 3600, "d": 86400, "m": 30 * 86400, "a": 365 * 86400, "y": 365 * 86400}.get(unit, 3600)


@app.route("/ack", methods=["GET", "POST"])
def ack():
    if request.method == "POST":
        host = request.form.get("host", "")
        services = request.form.get("services", ".*")
        duration_str = request.form.get("hours", "+4h")
        contact = request.form.get("contact", "web-user@localhost")
        message = request.form.get("message", "acknowledged via web")

        duration_secs = _parse_duration(duration_str)
        end_time = 0 if duration_secs == 0 else time.time() + duration_secs
        send_ack(host=host, services=services, end_time=end_time,
                 contact=contact, message=message)
        return redirect(url_for("host_detail", hostname=host))

    host = request.args.get("host", "")
    service = request.args.get("service", ".*")
    return render_template("ack.html", host=host, service=service)


@app.after_request
def refresh_cookies(response):
    if request.endpoint in ("set_lang", "set_theme"):
        return response
    lang = request.cookies.get("lang")
    if lang and lang in _SUPPORTED_LANGS:
        response.set_cookie("lang", lang, max_age=10 * 365 * 86400, samesite="Lax")
    theme = request.cookies.get("theme")
    if theme in ("light", "dark"):
        response.set_cookie("theme", theme, max_age=10 * 365 * 86400, samesite="Lax")
    return response


@app.route("/set-theme/<theme>")
def set_theme(theme):
    if theme not in ("light", "dark"):
        theme = "light"
    resp = redirect(request.referrer or url_for("index"))
    resp.set_cookie("theme", theme, max_age=10 * 365 * 86400, samesite="Lax")
    return resp


@app.route("/set-lang/<lang>")
def set_lang(lang):
    if lang not in _SUPPORTED_LANGS:
        lang = "es"
    resp = redirect(request.referrer or url_for("index"))
    resp.set_cookie("lang", lang, max_age=10 * 365 * 86400, samesite="Lax")
    return resp


@app.route("/ack-del/<ack_id>")
def ack_del(ack_id):
    send_ack_del(ack_id)
    referrer = request.referrer or url_for("index")
    return redirect(referrer)


@app.route("/api/status")
def api_status():
    """JSON API for status data."""
    hosts = config.get_hosts()
    result = {}
    for hostname in hosts:
        services = database.load_all_services(hostname)
        result[hostname] = {
            svc_name: {
                "color": svc.color,
                "summary": svc.summary,
                "report_time": svc.report_time,
                "duration": svc.duration,
            }
            for svc_name, svc in services.items()
        }
    return jsonify(result)


@app.route("/api/service/<hostname>/<service>")
def api_service(hostname, service):
    svc = database.load_service(hostname, service)
    if not svc:
        return jsonify({"error": "not found"}), 404
    acks = database.load_acks(hostname)
    color = svc.color
    if color not in ("green", "clear", "blue"):
        if any(ack.covers(service) for ack in acks):
            color = "blue"
    return jsonify({
        "color": color,
        "summary": svc.summary,
        "message": svc.message,
        "report_time": svc.report_time,
        "duration": svc.duration,
    })


@app.route("/api/check/<hostname>/<service>", methods=["POST"])
def api_check(hostname, service):
    """Ejecuta el plugin de red on-demand y devuelve el nuevo estado."""
    import importlib
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    from spong.status_sender import send_status as _send_status

    if hostname not in config.get_hosts():
        return jsonify({"error": "unknown host"}), 404

    # Cargar plugin de red
    try:
        mod = importlib.import_module(f"spong.plugins.network.{service}")
        func = getattr(mod, f"check_{service}", None)
    except ImportError:
        func = None

    if not func:
        return jsonify({"error": "no_plugin"}), 400

    # Ejecutar con timeout para evitar que la request cuelgue indefinidamente
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(func, hostname)
            color, summary, message = future.result(timeout=35)
    except FuturesTimeout:
        color, summary, message = "yellow", "check timeout", ""
    except Exception as e:
        color, summary, message = "red", f"check error: {e}", ""

    _send_status(hostname, service, color, summary, message)

    # Pequeña pausa para que spong-server persista el resultado
    time.sleep(0.3)

    svc = database.load_service(hostname, service)
    acks = database.load_acks(hostname)
    final_color = color
    if svc:
        final_color = svc.color
        if final_color not in ("green", "clear", "blue"):
            if any(ack.covers(service) for ack in acks):
                final_color = "blue"

    # Usamos time.time() directamente: acabamos de ejecutar el check ahora mismo
    return jsonify({
        "color": final_color,
        "summary": summary,
        "message": message,
        "report_time": time.time(),
        "checked": True,
    })


def _uptime_stats(host: str, service: str, periods_days: list[int]) -> dict:
    """Calculate uptime % for a service over multiple periods.

    Returns dict: { days: pct|None }
    "Up" = green or yellow. "Down" = red. purple/clear = excluded from total.
    """
    import re as _re
    from pathlib import Path as _Path

    now = time.time()
    max_days = max(periods_days)
    cutoff = now - max_days * 86400

    history_file = _Path(f"/usr/local/spong/var/database/{host}/history/current")
    events = []  # list of (timestamp, color)

    if history_file.exists():
        try:
            for line in history_file.read_text().splitlines():
                parts = line.split(None, 4)
                if len(parts) < 4 or parts[0] != "status":
                    continue
                ts, svc, color = int(parts[1]), parts[2], parts[3]
                if svc == service and ts >= cutoff:
                    events.append((ts, color))
        except Exception:
            pass

    # Add current state as final sentinel
    svc_dir = _Path(f"/usr/local/spong/var/database/{host}/services")
    current_color = None
    for f in svc_dir.glob(f"{service}-*"):
        current_color = f.name.split("-", 1)[1]
        try:
            m = _re.search(r"^timestamp \d+ (\d+)", f.read_text())
            ts = int(m.group(1)) if m else int(now)
        except Exception:
            ts = int(now)
        events.append((ts, current_color))
        break

    if not events:
        return {d: None for d in periods_days}

    events.sort()
    # Append "now" as a virtual event to close the last interval
    events.append((int(now), events[-1][1]))

    result = {}
    for days in periods_days:
        period_start = now - days * 86400
        up = 0.0
        total = 0.0
        for i in range(len(events) - 1):
            ts, color = events[i]
            next_ts = events[i + 1][0]
            seg_start = max(ts, period_start)
            seg_end = min(next_ts, now)
            if seg_end <= seg_start:
                continue
            duration = seg_end - seg_start
            if color in ("green", "yellow", "blue"):
                up += duration
                total += duration
            elif color == "red":
                total += duration
            # purple/clear: excluded (no data → don't penalize)
        result[days] = round(up / total * 100, 1) if total > 0 else None
    return result


@app.route("/uptime")
def uptime_page():
    groups   = config.get_groups()
    hosts_cfg = config.get_hosts()
    periods  = [1, 7, 30]

    group_rows = []
    for gname, gdata in groups.items():
        if not gdata.get("display", True):
            continue
        rows = []
        for hostname in gdata.get("members", []):
            if hostname not in hosts_cfg:
                continue
            services = [s for s, _ in config.host_services(hostname)]
            for svc in services:
                stats = _uptime_stats(hostname, svc, periods)
                # current color
                cur_color = "clear"
                from pathlib import Path as _P
                for f in _P(f"/usr/local/spong/var/database/{hostname}/services").glob(f"{svc}-*"):
                    cur_color = f.name.split("-", 1)[1]
                    break
                rows.append({
                    "host": hostname,
                    "service": svc,
                    "color": cur_color,
                    "stats": stats,
                })
        if rows:
            group_rows.append({"name": gdata.get("name", gname), "rows": rows})

    return render_template("uptime.html", group_rows=group_rows, periods=periods)


@app.route("/api/problems")
def api_problems():
    hosts = config.get_hosts()
    issues = []
    for hostname in hosts:
        services = database.load_all_services(hostname)
        for svc_name, svc in services.items():
            if svc.color in ("red", "yellow", "purple"):
                issues.append({
                    "host": hostname,
                    "service": svc_name,
                    "color": svc.color,
                    "summary": svc.summary,
                    "duration": svc.duration,
                })
    return jsonify(issues)


@app.route("/rrd/<hostname>/<service>.png")
def rrd_graph(hostname, service):
    from spong import rrd as _rrd
    period = request.args.get("period", "24h")
    width  = int(request.args.get("w", 500))
    height = int(request.args.get("h", 120))
    data = _rrd.graph_png(hostname, service, period, width, height)
    if not data:
        return Response("Sin datos RRD", status=404)
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=True)
