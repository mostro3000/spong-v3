# Auditoría de código SPONG — julio 2026

Revisión de bugs y robustez sobre SPONG v3.6.5, con los arreglos aplicados en
las releases **v3.6.6 → v3.7.1**. Este documento consolida los hallazgos por
área, marca cuáles se corrigieron (y en qué versión) y cuáles quedan pendientes.

- **Fecha:** 2026-07-04
- **Base auditada:** v3.6.5
- **Áreas revisadas:** núcleo del servidor, UI Flask (`web/app.py`), panel
  `/config` + integración SGT, generación de RRD (`spong/rrd.py`), agentes y
  plugins de red/cliente.

## Resumen

| | Cantidad |
|---|---|
| Hallazgos totales | ~60 |
| Corregidos (v3.6.6–v3.7.1) | 22 |
| Pendientes | ~38 (mayormente media/baja) |

Los **cuatro críticos de seguridad/datos** ya están corregidos: path traversal
por hostname, escritura de estado no atómica, borrado de historial de ping ante
fallo transitorio de rrdtool, y fuga de hashes de contraseña vía el historial de
configuración. Además se cerró **CSRF** en toda la UI y se hicieron **atómicas**
las escrituras de YAML de config y de `sgt_links.json`.

---

## Corregido

### v3.6.6 — Plugins con checks que reportaban mal

| Hallazgo | Archivo | Detalle |
|---|---|---|
| Expiración de certificado = código muerto | `plugins/network/https.py` | Con `CERT_NONE`, `getpeercert()` devuelve `{}`; un cert vencido daba verde para siempre. Se lee el DER y se parsea `notAfter` con `cryptography`. |
| Backend colgado tras handshake TLS → verde | `plugins/network/https.py` | Se separó el timeout del handshake del de la petición: si el TLS negocia pero el backend no responde el GET, ahora es rojo. |
| DNS no consultaba al host objetivo | `plugins/network/dns.py` | Hacía `getaddrinfo` contra el resolver local; reescrito con query UDP directa a `host:53` (configurable `dns.query_name`). |
| `processes` enviaba estado como `jobs` | `plugins/client/processes.py` | Copy-paste; dejaba `processes` stale y pisaba `jobs`. |
| `temp_ext` de UPS con normalización inconsistente | `plugins/network/_ups_snmp.py` | `÷10` sólo si `raw>1000` → 25 °C daba rojo falso permanente. Ahora `temp_ext`/`freq` se dividen siempre por 10 (décimas, como la MIB APC). |
| Parsing dependiente de locale | `safe_exec.py` | Sin `LC_ALL=C`, en locale español `uptime` no matcheaba → `load=0.0` verde falso. Se fuerza `LC_ALL=C`/`LANG=C`. |

### v3.6.7 — Seguridad y robustez del servidor

| Hallazgo | Severidad | Archivo | Detalle |
|---|---|---|---|
| Path traversal por hostname `.`/`..` | **Alta** | `protocol.py`, `server.py`, `database.py`, `rrd.py`, `web/app.py` | `VALID_HOST_RE` aceptaba `.`/`..`. Se agregó `valid_host()`/`valid_service()` en `parse_update` (status/ack/ack-del) y en el handler BigBrother; defensa en profundidad en `database`, `rrd._rrd_dir` y la ruta `/rrd/<host>`. |
| `save_status` no atómico ni con lock | **Alta** | `database.py` | Borraba los 5 archivos de color y escribía sin lock → estados fantasma / servicio "desaparecido". Ahora escritura atómica (tmp + `os.replace`) serializada por lock por `(host, service)`. |
| `_update_ping` borra el RRD ante fallo transitorio | **Alta** | `rrd.py` | `_rrd_ds_count()` devolvía `0` tanto en 0-DS como en error → `os.remove` de un RRD sano (hasta 720 días de historial). Ahora devuelve `-1` en error y sólo migra ante conteo positivo `<4`. |
| Fuga de hashes vía `/config/history/users` | **Alta** | `web/config_admin.py` | Sólo pedía auth, no el permiso `users`. Ver/restaurar snapshots de `users` ahora exige `users`; el índice oculta esas entradas. |

### v3.6.8 — CSRF

| Hallazgo | Archivo | Detalle |
|---|---|---|
| Sin protección CSRF (Basic Auth) | `web/app.py`, `web/config_admin.py`, plantillas | El navegador reenviaba credenciales en POST cross-site. Se implementó **double-submit cookie**: token aleatorio en cookie `HttpOnly`/`SameSite=Lax`, campo oculto en formularios + `<meta>` para fetch, revalidado con `hmac.compare_digest`. Cubre `/ack`, `/sgt-ticket`, `/api/check` (header) y todo `/config`. |

### v3.6.9 — Escritura atómica de YAML y `sgt_links.json`

| Hallazgo | Archivo | Detalle |
|---|---|---|
| `_save_yaml` con tmp de nombre fijo, sin lock | `web/config_admin.py` | Dos guardados del mismo archivo entremezclaban el `.tmp` → YAML corrupto. Ahora tmp único (`mkstemp`) + `fsync` + `os.replace`, con lock por ruta. Igual para snapshots e `log.json` (cuyo read-modify-write perdía entradas). |
| Carrera en `sgt_links.json` (doble ticket) | `web/sgt_link.py` | El dedup de `crear_ticket` (leer→POST→leer→guardar) sin lock → doble clic creaba dos tickets. Ahora todo bajo `flock` (cross-process web ↔ `spong-sgt-sync`), escritura atómica, y ya no traga errores de disco en silencio. |

### v3.7.0 / v3.7.1 — Feature: dashboard TUI

No son correcciones, sino una feature nueva: `spong/tui.py` (comandos `spong top`
/ `spong-tui`, alias global `s`). Vista curses en vivo que lee `var/database` +
`etc/*.yaml` y replica las reglas de color de la web. Documentada en el README
(§8b).

---

## Pendiente

Ordenado por prioridad sugerida. Ninguno es de explotación trivial ni de pérdida
de datos crítica (esos ya se cerraron), pero varios afectan robustez operativa.

### Servidor / base de datos

| Hallazgo | Severidad | Archivo | Nota |
|---|---|---|---|
| Config cargada una sola vez (sin SIGHUP) | Media | `server.py` | El `stale_data_scanner` puede borrar archivos de estado de un servicio recién configurado desde `/config` hasta reiniciar `spong-server`. Falta recarga por SIGHUP o mtime. |
| `stale_data_scanner` muere en silencio | Media | `server.py` | El `while True` no tiene try/except; un `OSError` transitorio mata la task y nunca más se marca purple ni se limpia. |
| Cuerpo del mensaje puede truncarse | Media | `server.py` | `reader.read(100_000)` retorna con el primer segmento TCP; un detalle grande en dos paquetes se guarda a medias. Leer en bucle hasta EOF. |
| `float(ts)` sin try en `parse_update` | Media | `protocol.py` | Un timestamp malformado (`status h svc red : x`) lanza `ValueError` no manejado ("Task exception was never retrieved"). |
| `handle_query` puede fugar el socket | Media | `server.py` | Sin `finally` que cubra todo el handler; una línea de >64 KB o un error en `dispatch_query` deja el transporte abierto. |
| `archive_old_history` pierde entradas concurrentes | Media | `database.py` | El cleanup relee→trunca mientras el server apendea; ventana read-modify-write. |
| `remove_stale_services` sin try aborta el cleanup | Media | `database.py` | `FileNotFoundError` durante el barrido mata el proceso de cleanup entero. |
| TTL del protocolo calculado y descartado | Baja | `database.py` | La expiración por TTL nunca funciona (`expire_time` queda en 0). |
| `write_pid` tras daemonizar muere en silencio | Baja | `server.py` | Sin crear el dir de `tmp`; en instalación nueva el daemon sale sin dejar rastro. |
| `Acknowledgment.covers()` evalúa regex de red | Baja | `models.py` | `services` llega crudo; riesgo bajo de ReDoS. Validar con `VALID_SVC_RE`. |

### UI Flask (`web/app.py`)

| Hallazgo | Severidad | Nota |
|---|---|---|
| `int(w/h)` sin protección en `/rrd` | Media | `?w=abc` → HTTP 500 en vez de 400. |
| `host_detail`/`service_detail` no validan host contra config | Baja | Dependen del converter de Werkzeug; conviene validar como hace `api_check`. |
| Carrera en la caché del dashboard | Baja | Reconstruye fuera del lock y guarda con `ts=now`; puede resucitar una invalidación (un ack tarda hasta el TTL en verse). |
| `time.sleep(0.3)` como sincronización | Baja | El "verificar ahora" puede reflejar estado viejo si el server tarda. |
| `ack` POST con host vacío → ack huérfano | Baja | Falta validar host no vacío. |
| Color en uptime por `split("-")` | Baja | Frágil ante nombres de servicio con guion. |

### Panel `/config` + SGT

| Hallazgo | Severidad | Nota |
|---|---|---|
| Rename de host sin rollback | Media | Si `_merge_tree` falla a mitad (p. ej. permisos en `var/rrd`), `database` ya se movió pero `hosts.yaml` no se guarda → estado inconsistente. |
| "Nuevo grupo" sobrescribe uno existente | Media | Un editor/admin que crea un grupo con clave ya existente lo pisa sin aviso. |
| Contraseña en texto plano soportada | Baja | `auth_utils` aún compara `password` plano; conviene warning/migración. |
| Snapshots de `users` sin permisos 0600 | Baja | `HISTORY_DIR` sin `mode` explícito; los snapshots contienen `password_hash`. |
| `logout` no invalida credenciales Basic | Baja | Limitación conocida de Basic Auth; documentarla. |

### RRD (`spong/rrd.py`)

| Hallazgo | Severidad | Nota |
|---|---|---|
| `get_rrd_name()` read-modify-write sin lock | Media | Updates concurrentes del mismo host pisan el name-map; historial de disco partido. |
| `if not info` no detecta returncode≠0 | Media | Un `CompletedProcess` fallido es truthy; genera comandos rrdtool malformados. |
| `_sanitize_name` no neutraliza `:` | Media | Un mountpoint con `:` corrompe el name-map y rompe el gráfico de disco del host. |
| `_update_memory` toma dos `%` cualesquiera | Media | Fallback frágil grafica datos basura. |
| Sin `--no-overwrite` en `create` | Media | Carrera check-then-create puede pisar un RRD. |
| `_run()` sin `timeout` | Baja | Un rrdtool colgado (NFS) bloquea el worker. |
| `float()` sobre `[\d.]+` | Baja | `"1.2.3"` lanza y aborta el update del servicio. |
| Gráficos apilados descartan stderr / sin clamp de altura | Baja | Paneles que desaparecen sin rastro; `?h=20` puede romper el gráfico. |
| Catch-all sin traceback | Baja | `log.error("%s")` sin `log.exception`. |

### Agentes / plugins

| Hallazgo | Severidad | Nota |
|---|---|---|
| `send_status` dentro del lock global | Media | Con el server caído, cada check retiene el lock hasta 30 s × N servidores → el ciclo se serializa. |
| `interfaces.ignore_interfaces` como string se itera por carácter | Media | `"Null0"` agrega `{n,u,l,0}` al set; la interfaz no se ignora. |
| `chronyc` matchea `"506"` por substring | Media | Un offset con "506" dispara rojo falso. |
| `speedtest` accede a claves fuera del try | Media | Un test abortado → `KeyError` y el servicio queda stale. |
| SNMP INTEGER sin signo | Baja | Un negativo se lee como positivo gigante. |
| `_camara`/`wassoc`/`wuptime` recv sin tope ni deadline | Baja | Un dispositivo que gotea bytes retiene un worker. |
| `termica`/`presence` devuelven rojo en vez de purple | Baja | Rompen la convención v3.6.1 de purple para "sin respuesta". |
| `email_plugin.communicate()` sin timeout | Baja | Un sendmail colgado bloquea las notificaciones. |
| `StrictHostKeyChecking=no` en SSH | Baja | Acepta MITM; usar `accept-new`. |

---

## Notas operativas transversales

- **Rotar el token de GitHub**: el PAT está embebido en texto plano en la URL del
  remoto `origin` (`git remote -v`). Conviene rotarlo y usar un credential helper.
- **Modo de arranque de `spong-web`**: el service corre `python3 web/app.py`
  (Flask dev server, un proceso multihilo). Los locks de hilos de `_save_yaml`
  alcanzan bajo ese modo; si se migrara a gunicorn multi-worker habría que pasar
  a `flock` como en `sgt_link`.
- **Despliegue**: tras instalar el `.deb`, reiniciar `spong-server` (handlers y
  `save_status`) y `spong-web` (CSRF, YAML atómico). El check `dns` cambió de
  semántica (liveness del servidor DNS del host); verificar que ningún host DNS
  pase de verde a rojo por un firewall que bloquee UDP/53 saliente.
