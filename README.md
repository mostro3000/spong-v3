# SPONG v3.4.0 — Network & Services Monitor

**SPONG** (Simple Preventive Operations Network Guardian) is a network and services monitoring system originally written in Perl. v3 is a complete rewrite in Python 3, keeping full compatibility with the original database and configuration files.

> **Features:** multi-group host matrix · RRD graphs (SmokePing-style ping) · ACK/acknowledgements · 7-language UI · dark mode · mobile-responsive UI · historical uptime % · on-demand service checks · .deb packages · migration script from Perl config

[![Build .deb](https://github.com/mostro3000/spong-v3/actions/workflows/build-deb.yml/badge.svg)](https://github.com/mostro3000/spong-v3/actions/workflows/build-deb.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

---

> _Documentación completa en español a continuación._

---

## Capturas de pantalla

| Vista de grupos (modo claro) | Modo oscuro |
|---|---|
| ![Grupos](docs/screenshots/01_grupos.png) | ![Dark mode](docs/screenshots/04_dark_mode.png) |

| Vista de host (riego-patio: temp/hum/co2) | Página de problemas |
|---|---|
| ![Host](docs/screenshots/02_host.png) | ![Problemas](docs/screenshots/03_problemas.png) |

| Grupo Clima (sensores IoT + SSH) |
|---|
| ![Clima](docs/screenshots/05_clima.png) |

| Gráfico ping estilo SmokePing (lightbox) | Gráfico HTTP tiempo de respuesta (lightbox) |
|---|---|
| ![Ping graph](docs/screenshots/06_ping_graph.png) | ![HTTP graph](docs/screenshots/07_http_graph.png) |

### Vista mobile (responsive)

| Grupos (modo claro) | Sidebar / menú | Detalle de host |
|---|---|---|
| ![Mobile grupos](docs/screenshots/mobile_01_grupos.png) | ![Mobile sidebar](docs/screenshots/mobile_02_sidebar.png) | ![Mobile host](docs/screenshots/mobile_05_host.png) |

| Grupos (modo oscuro) | Sidebar (modo oscuro) |
|---|---|
| ![Mobile dark](docs/screenshots/mobile_03_dark_grupos.png) | ![Mobile dark sidebar](docs/screenshots/mobile_04_dark_sidebar.png) |

---

## Instalación rápida

### Servidor (Debian / Ubuntu)

```bash
# 1. Descargar el .deb desde Releases
wget https://github.com/mostro3000/spong-v3/releases/latest/download/spong-server_3.4.0-1_all.deb

# 2. Instalar (el postinst configura dependencias pip y activa los 4 servicios systemd)
dpkg -i spong-server_3.4.0-1_all.deb

# 3. Editar la configuración
nano /usr/local/spong/etc/spong.yaml    # servidor, thresholds, checks
nano /usr/local/spong/etc/hosts.yaml    # hosts a monitorear
nano /usr/local/spong/etc/groups.yaml   # grupos de hosts

# 4. Reiniciar para que tome la config
systemctl restart spong-server spong-network spong-client spong-web

# 5. Abrir la interfaz web
xdg-open http://localhost:8090/
```

### Cliente remoto (en otro host)

```bash
wget https://github.com/mostro3000/spong-v3/releases/latest/download/spong-client_3.4.0-1_all.deb
dpkg -i spong-client_3.4.0-1_all.deb   # instalación interactiva: pregunta servidor, hostname, checks
```

### Migración desde SPONG Perl (spong.conf / spong.hosts / spong.groups)

```bash
cd /etc/spong/   # o donde estén los archivos viejos
python3 /usr/local/spong/bin/spong-migrate.py --all --outdir /usr/local/spong/etc/
```

---

## Índice

1. [Arquitectura general](#1-arquitectura-general)
2. [Procesos / Daemons](#2-procesos--daemons)
3. [Arranque y parada](#3-arranque-y-parada)
4. [Configuración](#4-configuración)
5. [Plugins del cliente (checks locales)](#5-plugins-del-cliente-checks-locales)
6. [Plugins de red (checks remotos)](#6-plugins-de-red-checks-remotos)
7. [Colores de estado](#7-colores-de-estado)
8. [Interfaz web](#8-interfaz-web)
9. [Gráficos RRD](#9-gráficos-rrd)
10. [Reconocimientos (ACKs)](#10-reconocimientos-acks)
11. [Base de datos](#11-base-de-datos)
12. [Logs](#12-logs)
13. [Mantenimiento](#13-mantenimiento)
14. [Empaquetado .deb](#14-empaquetado-deb)
15. [GitHub Actions (CI)](#15-github-actions-ci)
16. [Historial de cambios](#16-historial-de-cambios)

---

## 1. Arquitectura general

```
┌─────────────────────────────────────────────────────────────┐
│                        SPONG Server                          │
│                   (spong/server.py :1998)                    │
│  - Recibe actualizaciones de estado via TCP                  │
│  - Escribe en base de datos                                  │
│  - Actualiza RRDs                                            │
│  - Ejecuta scanner de servicios stale                        │
└──────────┬────────────────────────┬────────────────────────--┘
           │ TCP :1998              │ TCP :1998
           │                        │
┌──────────▼──────────┐   ┌─────────▼──────────────┐
│   Network Agent      │   │    Client Agent          │
│ (spong/network_      │   │  (spong/client_agent.py) │
│  agent.py)           │   │                          │
│                      │   │  Corre en el host local  │
│  Chequea hosts        │   │  (s2) y ejecuta plugins  │
│  remotos via red:     │   │  locales: disk, cpu,     │
│  ping, http, ssh,     │   │  memory, jobs, sensors,  │
│  mysql, snmp, dns...  │   │  hddtemp, uptime, logs   │
└─────────────────────┘   └──────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                     Interfaz Web (Flask)                     │
│              (web/app.py - puerto 8090)                      │
│  - Lee directamente de la base de datos                      │
│  - Muestra estado, gráficos RRD, historial                   │
│  - Gestiona reconocimientos                                  │
└─────────────────────────────────────────────────────────────┘
```

**Flujo de datos:**
1. El **Network Agent** hace ping/http/ssh/etc. a los hosts remotos
2. El **Client Agent** ejecuta checks locales (disk, cpu, etc.) en el host donde corre
3. Ambos envían resultados al **Server** via TCP (puerto 1998)
4. El **Server** guarda los datos en la base de datos y actualiza los RRDs
5. La **Interfaz Web** lee la base de datos y muestra el estado

---

## 2. Procesos / Daemons

| Proceso | Archivo | Puerto | Descripción |
|---------|---------|--------|-------------|
| `spong-server` | `spong/server.py` | TCP 1998 (recepción) | Servidor central. Recibe updates, escribe DB, actualiza RRDs, escanea stale |
| `spong-network` | `spong/network_agent.py` | — | Agente de red. Chequea hosts remotos via ping, http, ssh, etc. |
| `spong-client` | `spong/client_agent.py` | — | Agente local. Ejecuta checks en el host donde corre (s2) |
| `spong-web` | `web/app.py` | TCP 8090 | Interfaz web Flask |

---

## 3. Arranque y parada

SPONG arranca automáticamente al iniciar el sistema operativo mediante **systemd**. Los unit files están en `/etc/systemd/system/spong-*.service`.

### Comandos systemd

```bash
# Estado de todos los servicios
systemctl status spong-server spong-network spong-client spong-web

# Arrancar / parar / reiniciar un servicio
systemctl start   spong-server
systemctl stop    spong-network
systemctl restart spong-web

# Reiniciar todos
systemctl restart spong-server spong-network spong-client spong-web

# Habilitar / deshabilitar arranque automático
systemctl enable  spong-server   # ya habilitado
systemctl disable spong-network  # si se quiere deshabilitar
```

### Logs via journald

```bash
journalctl -u spong-server  -f        # seguir log en tiempo real
journalctl -u spong-network --since "1 hour ago"
journalctl -u spong-web     -n 50     # últimas 50 líneas
```

Los logs también se guardan en `/var/log/spong-*.log` (append).

### Dependencias de arranque

```
spong-server   ← arranca primero (necesita red)
spong-network  ← requiere spong-server
spong-client   ← requiere spong-server
spong-web      ← requiere spong-server
```

### Aplicar cambios de código o configuración

Los procesos cargan el código y la configuración **solo al arrancar**. Luego de editar cualquier archivo `.py` o `.yaml` hay que reiniciar el proceso afectado:

```bash
systemctl restart spong-network   # después de editar plugins de red o rrd.py... ver tabla
```

| Archivo modificado | Proceso a reiniciar |
|--------------------|---------------------|
| `spong/plugins/network/*.py` | `spong-network` |
| `spong/plugins/client/*.py` | `spong-client` |
| `spong/rrd.py`, `spong/server.py` | `spong-server` |
| `web/app.py`, `web/templates/` | `spong-web` |
| `etc/hosts.yaml`, `etc/groups.yaml` | todos |
| `etc/spong.yaml` | proceso afectado |
| `/etc/apache2/sites-available/*.conf` | `apache2` (`systemctl restart apache2`) |

> **Nota:** `spong/rrd.py` es importado por `spong-server`. Los cambios en `rrd.py` requieren reiniciar `spong-server`, no `spong-network`.

La **interfaz web** re-lee `spong.yaml` en cada request pero requiere reinicio para cambios en `hosts.yaml` o `groups.yaml`.

---

## 4. Configuración

Todos los archivos de configuración están en `/usr/local/spong/etc/`.

### 4.1 `spong.yaml` — Configuración principal

```yaml
server:
  host: "s2"          # hostname del servidor SPONG
  update_port: 1998   # puerto donde el server recibe updates
  query_port: 1999    # puerto de queries (legacy)
  bb_port: 1984       # puerto BigBrother (legacy)
  alarm_timeout: 10   # timeout en segundos para alarmas

database:
  path: "/usr/local/spong/var/database"
  archive_path: "/usr/local/spong/var/archives"

sleep:
  default: 300          # segundos entre ciclos (default)
  spong-client: 500     # ciclo del cliente local
  spong-network: 300    # ciclo del agente de red

network:
  crit_warn_level: 1    # reintentos antes de reportar crítico
  recheck_sleep: 15     # segundos entre reintentos

# Plugins del cliente a ejecutar
checks: "disk diski cpu jobs logs memory sensors hddtemp uptime"

thresholds:
  disk:
    warn:
      ALL: 90       # umbral warn para todos los filesystems
      /usr: 95      # umbral específico para /usr
    crit:
      ALL: 95
  cpu:
    warn: 7.0       # load average warn
    crit: 8.0
  memory:
    warn: 90        # % uso físico warn
    crit: 95
  hddtemp:
    warn: 50        # °C
    crit: 60

# Procesos que el check "jobs" debe verificar
processes:
  crit:             # si falta → rojo
    - fauxmo
    - rtl_tcp
    - motion
    - mqttorrd
    - asterisk
    - lighttpd
  warn: []          # si falta → amarillo

web:
  auth_user: "spong"        # Basic Auth (vacío = sin auth)
  auth_password: "spong123"

cleanup:
  old_service_days: 20   # días hasta borrar servicios sin datos
  old_history_days: 30   # días de historial a conservar
```

### 4.2 `hosts.yaml` — Definición de hosts

```yaml
contacts:
  mt:
    name: "mt"
    email: "mt@localhost"

hosts:
  s2:
    services: "ping: http ssh mysql snmp disk diski cpu jobs memory sensors hddtemp uptime"
    contact: "mt"
    ip_addr: ["192.168.0.11"]

  cam3d:
    services: "ping"
    contact: "mt"
    ip_addr: ["192.168.0.132"]
```

**Formato de servicios:**
- Los servicios se listan separados por espacios
- El `:` después de un servicio significa **stop_after**: si ese servicio falla, no se chequean los siguientes. Ejemplo: `ping: http ssh` — si ping falla, no se intenta http ni ssh
- El orden importa: determina el orden de visualización en la interfaz web
- Los servicios del **cliente local** (disk, cpu, memory, jobs, etc.) deben estar en la lista del host donde corre `spong-client` (actualmente `s2`)

### 4.3 `groups.yaml` — Grupos de hosts

```yaml
groups:
  servers:
    name: "Servers"
    members:
      - s2
      - mt0
      - disco-mt
    display: true
    compress: true   # no usado actualmente, todos usan vista matriz
```

Los grupos se muestran en la interfaz web en el orden en que aparecen en este archivo.

---

## 5. Plugins del cliente (checks locales)

El `spong-client` ejecuta estos plugins en el host local (`s2`). Se configuran en `spong.yaml` → `checks`.

| Plugin | Servicio | Qué mide | Umbrales en spong.yaml |
|--------|----------|----------|------------------------|
| `disk.py` | `disk` | Uso de filesystems (`df`) | `thresholds.disk.warn/crit` |
| `diski.py` | `diski` | Uso de inodos (`df -i`) | `thresholds.disk.warn/crit` |
| `cpu.py` | `cpu` | Load average y jobs | `thresholds.cpu.warn/crit` |
| `jobs.py` | `jobs` | Procesos requeridos corriendo | `processes.crit/warn` |
| `memory.py` | `memory` | Uso de memoria RAM | `thresholds.memory.warn/crit` |
| `sensors.py` | `sensors` | Temperatura CPU/cores (lm-sensors) | `sensor_thresholds.*` |
| `hddtemp.py` | `hddtemp` | Temperatura de discos (`hddtemp`) | `thresholds.hddtemp.warn/crit` |
| `uptime.py` | `uptime` | Uptime del sistema | — |
| `logs.py` | `logs` | Patrones en archivos de log | `log_checks[]` en spong.yaml |

**Agregar un host al monitoreo local:** agregar los servicios correspondientes en `hosts.yaml` para ese host.

---

## 6. Plugins de red (checks remotos)

El `spong-network` ejecuta estos plugins contra los hosts remotos configurados en `hosts.yaml`.

| Plugin | Servicio | Qué chequea |
|--------|----------|-------------|
| `ping.py` | `ping` | Conectividad ICMP — 10 pings por ciclo, reporta min/avg/max/loss |
| `http.py` | `http` | HTTP GET, código de respuesta, tiempo de respuesta |
| `https.py` | `https` | HTTPS GET con fallback SSL legacy y TCP para certs débiles |
| `ssh.py` | `ssh` | Conexión TCP puerto 22, banner SSH, tiempo de respuesta |
| `mysql.py` | `mysql` | Conexión TCP puerto 3306, tiempo de respuesta |
| `snmp.py` | `snmp` | Consulta SNMPv1 (sysDescr del equipo) |
| `dns.py` | `dns` | Resolución DNS |
| `telnet.py` | `telnet` | Conexión TCP puerto 23 |
| `ftp.py` | `ftp` | Conexión TCP puerto 21 |
| `smtp.py` | `smtp` | Conexión SMTP puerto 25 |
| `imap.py` | `imap` | Conexión IMAP puerto 143 |
| `ntp.py` | `ntp` | Servidor NTP |
| `temp.py` | `temp` | Temperatura: lee JSON local en `/var/www/html/` o via SSH JSON (ej: `riego-patio`) |
| `hum.py` | `hum` | Humedad: lee JSON local o via SSH JSON |
| `co2.py` | `co2` | Calidad del aire via SSH JSON: eCO2 (ppm), TVOC (ppb), AQI (0–5) |
| `rcpu.py` | `rcpu` | CPU de router MikroTik via SNMP (`hrProcessorLoad` + OID MikroTik) |
| `rtemp.py` | `rtemp` | Temperatura de router MikroTik via SNMP (placa y CPU en °C) |
| `scpu.py` | `scpu` | CPU de switch TP-Link JetStream via SNMP |
| `macs.py` | `macs` | Cantidad de MACs aprendidas via SNMP walk (`dot1dTpFdbTable`) |
| `termica.py` | `termica` | Llaves térmicas Tuya: tensión, corriente, potencia, energía, corriente de fuga |
| `rtsp.py` | `rtsp` | Disponibilidad de cámara: prueba RTSP/554 con OPTIONS estándar, fallback Tapo/2020 |
| `soil.py` | `soil` | Sensores de suelo via SSH JSON: humedad de pasto/canteros, lluvia, válvulas |
| `ruptime.py` | `ruptime` | Uptime via SSH para hosts sin spong-client (caché 55s) |
| `ups.py` | `ups` | UPS APC via SNMP: tensión entrada/salida, frecuencia, temperatura batería/exterior |
| `interfaces.py` | `interfaces` | Interfaces de red caídas via SNMP (admin up / oper down) |
| `nfs.py` | `nfs` | Disponibilidad NFS via `rpcinfo -p` (nfsd + mountd) |

**Detalles de plugins SNMP:**

- **`snmp.py`** implementa SNMPv1 desde cero (sin librerías externas). Expone:
  - `check_snmp()` — GET sysDescr, muestra descripción del equipo
  - `snmp_get_int()` — GET de cualquier OID, devuelve entero
  - `snmp_walk_count()` — GETNEXT iterativo, cuenta entradas en un subtree

- **`rcpu.py`** prueba primero `hrProcessorLoad.1` (estándar) y cae a `mtxrSystemCpuLoad` (MikroTik). Umbrales: ≥70% yellow, ≥90% red.

- **`rtemp.py`** lee `mtxrHlTemperature` (placa) y `mtxrHlCpuTemperature` (CPU). Valores en décimas de °C (e.g. 370 = 37.0°C). Umbrales: ≥70°C yellow, ≥85°C red. El OID de temperatura CPU se consulta 3 veces tomando el mínimo, para evitar falsos alarmas en modelos como RBcAPGi-5acD2nD que hacen round-robin entre sensores internos en un mismo OID (puede devolver 44.8°C, 67.2°C o 89.6°C en consultas sucesivas).

- **`scpu.py`** usa OID TP-Link JetStream (`1.3.6.1.4.1.11863.6.4.1.1.1.1.2.1`) con fallback a `hrProcessorLoad`.

- **`https.py`** intenta TLS moderno → TLS legacy (SECLEVEL=0, TLSv1 para equipos con certs RSA-1024) → TCP puro. Reporta verde si el puerto responde aunque el handshake SSL falle por clave débil.

**Sensores IoT via SSH JSON (`_ssh_json.py`):**

`temp`, `hum` y `co2` pueden leer datos de un host remoto via SSH en lugar de un archivo JSON local. Se configura en el diccionario `_SSH_MAP` de cada plugin:

```python
# En temp.py / hum.py / co2.py
_SSH_MAP = {
    "riego-patio": ("192.168.0.78", "/dev/shm/riepopi.json", ["air", "temperature_C"]),
}
```

El helper `_ssh_json.py` hace `ssh root@host cat /path/file.json` con un caché de 60s para evitar conexiones redundantes cuando múltiples plugins consultan el mismo host en el mismo ciclo.

**Llaves térmicas Tuya (`termica.py`):**

Lee directamente los dispositivos Tuya via `tinytuya` (protocolo local, sin cloud). Requiere configurar `/usr/local/spong/etc/termicas.yaml` (no incluido en el paquete por seguridad — se incluye `termicas.yaml.example` como plantilla):

```yaml
# /usr/local/spong/etc/termicas.yaml
devices:
  termica1:
    id: "DEVICE_ID"
    ip: "192.168.0.x"
    local_key: "LOCAL_KEY_16_CHARS"
    version: 3.5          # 3.3, 3.4 o 3.5 según firmware
    warn_A: 16.0
    crit_A: 20.0
    leak_warn_mA: 5.0
    leak_crit_mA: 30.0
```

Los `device_id` y `local_key` se obtienen con:
```bash
pip3 install tinytuya
python3 -m tinytuya scan
```

El plugin incluye un caché interno de 55 s para evitar saturar los dispositivos cuando SPONG consulta múltiples hosts en el mismo ciclo. La configuración se recarga automáticamente si el archivo cambia (comparación de mtime).

**Patrón host virtual:** Un mismo dispositivo físico puede aparecer en dos grupos con distintos roles. Por ejemplo, `riegopi` (IP 192.168.0.78) aparece en el grupo **Servers** con servicios `ping: ssh`, y `riego-patio` (misma IP) aparece en **Clima** con servicios `temp hum co2`. Esto permite separar los datos del sistema operativo del host de los datos de sensores ambientales.

**Stop_after (`:`):** Si `ping` falla en un host con `ping: http ssh`, el agente omite http y ssh automáticamente para ese ciclo.

**Recheck:** Cuando un servicio falla, el agente espera `recheck_sleep` segundos y reintenta hasta `crit_warn_level` veces antes de reportar el estado final. Esto evita falsos positivos por microcoeficiencias de red.

---

## 7. Colores de estado

| Color | Significado | Visualización |
|-------|-------------|---------------|
| 🟢 **green** | OK, funcionando normalmente | Verde |
| 🟡 **yellow** | Advertencia, umbral warn superado | Amarillo |
| 🔴 **red** | Crítico, servicio caído o umbral crit superado | Rojo |
| 🟣 **purple** | Stale: sin datos recientes (>1800s sin actualización) | Violeta |
| 🔵 **blue** | Reconocido: el problema fue acknowledgeado | Azul |
| ⚪ **clear** | Sin datos / estado desconocido | Gris |

**Prioridad:** red > purple > yellow > blue > green > clear

**Color de grupo:** El color más grave de todos los servicios del grupo. El azul (reconocido) se trata como verde a nivel de grupo — un grupo con solo verdes y azules muestra verde.

**Stale:** El servidor escanea periódicamente la base de datos. Si un servicio no recibió actualizaciones en más de 1800 segundos, lo marca como `purple`. Si el servicio no está en la configuración del host, lo elimina directamente.

---

## 8. Interfaz web

**URLs de acceso:**
- Directo (Flask): `http://s2:8090`
- Vía Apache: `http://s2/spong` (o cualquier hostname que apunte al servidor)

**Autenticación:** Basic Auth HTTP. Configurar en `spong.yaml` → `web.auth_user` / `web.auth_password`. Dejar `auth_user` vacío para deshabilitar.

### Páginas

| URL | Descripción |
|-----|-------------|
| `/` | Vista principal por grupos. Matriz host × servicio con círculos de colores. |
| `/host/<hostname>` | Detalle de un host: todos sus servicios, estado, tiempo en estado actual, último reporte, gráficos RRD. |
| `/service/<hostname>/<servicio>` | Detalle de un servicio específico con historial y gráficos. |
| `/problems` | Lista de todos los servicios con problemas (rojo, amarillo, violeta) ordenados por severidad. Incluye botón ACK directo. |
| `/acks` | Lista de reconocimientos activos con estado actual de los servicios reconocidos. |
| `/ack` | Formulario para crear un nuevo reconocimiento. |
| `/api/status` | JSON con el estado de todos los hosts y servicios. |
| `/api/problems` | JSON con solo los servicios con problemas. |
| `/rrd/<hostname>/<servicio>.png` | Imagen PNG del gráfico RRD. Parámetros: `period` (1h/24h/7d/30d/1y), `w`, `h`. |

### Proxy Apache (`/spong`)

SPONG está configurado para ser accesible desde Apache en `/spong` mediante reverse proxy. La configuración está en `/etc/apache2/sites-available/000-default.conf` (HTTP) y `default-ssl.conf` (HTTPS):

```apache
ProxyPass        /spong/ http://127.0.0.1:8090/
ProxyPassReverse /spong/ http://127.0.0.1:8090/
RequestHeader set X-Forwarded-Prefix /spong
```

Flask usa `werkzeug.middleware.proxy_fix.ProxyFix` para leer el header `X-Forwarded-Prefix` y ajustar `SCRIPT_NAME`, de modo que `url_for()` genera automáticamente URLs con el prefijo `/spong` cuando se accede vía Apache. Todos los links y URLs de los templates usan `url_for()` para que funcionen correctamente tanto en acceso directo (`:8090`) como vía Apache (`/spong`).

Módulos Apache requeridos: `proxy`, `proxy_http`, `headers` (habilitados con `a2enmod`).

### Características visuales

- **Auto-refresh:** cada 120 segundos con countdown visible en el header
- **Reloj en vivo:** actualizado cada segundo
- **Tooltips:** al pasar el mouse sobre un círculo de la matriz muestra el resumen del servicio
- **Gráficos toggle:** en la vista de host, el botón 📊 muestra/oculta los gráficos de cada servicio
- **Lightbox:** clic en cualquier gráfico lo amplía a 1200×300 px sobre fondo oscuro. Cerrar con clic o `Escape`
- **Sidebar:** muestra grupos con problemas (rojos), ordenados según `groups.yaml`
- **Formulario ACK con memoria:** el contacto, duración y mensaje del último reconocimiento se recuerdan via `localStorage` y se pre-rellenan en el próximo

### Columnas de servicios en la matriz

Los servicios se muestran en el orden definido en `hosts.yaml`. Pares relacionados se agrupan de forma adyacente:
- `http` → `https` (siempre juntos)
- `ssh` → `telnet` (siempre juntos)

---

## 9. Gráficos RRD

Los archivos RRD se guardan en `/usr/local/spong/var/rrd/<hostname>/`.

| Servicio | Archivo RRD | Datos graficados |
|----------|-------------|-----------------|
| `ping` | `ping-times.rrd` | min / avg / max (segundos) + % pérdida de paquetes |
| `disk` | `disk-<nombre>.rrd` | % uso y bytes usados por filesystem |
| `diski` | `diski-<nombre>.rrd` | % uso de inodos por filesystem |
| `cpu` | `la.rrd` | Load average, usuarios, jobs |
| `jobs` | `la.rrd` | Jobs activos (mismo RRD que cpu) |
| `memory` | `mem.rrd` | % uso de memoria física |
| `sensors` | `sensors.rrd` | Temperatura CPU (Package, Cores) en °C |
| `hddtemp` | `hddtemp.rrd` | Temperatura de discos en °C |
| `http` | `http-time.rrd` | Tiempo de respuesta HTTP (segundos) |
| `https` | `https-time.rrd` | Tiempo de respuesta HTTPS (segundos) |
| `ssh` | `ssh-time.rrd` | Tiempo de respuesta SSH (segundos) |
| `mysql` | `mysql-time.rrd` | Tiempo de respuesta MySQL (segundos) |
| `telnet` | `telnet-time.rrd` | Tiempo de respuesta TCP/23 (segundos) |
| `ftp` | `ftp-time.rrd` | Tiempo de respuesta FTP/21 (segundos) |
| `smtp` | `smtp-time.rrd` | Tiempo de respuesta SMTP/25 (segundos) |
| `imap` | `imap-time.rrd` | Tiempo de respuesta IMAP/143 (segundos) |
| `ntp` | `ntp-time.rrd` | Tiempo de respuesta NTP (segundos) |
| `rcpu` | `rcpu.rrd` | % CPU router (SNMP) |
| `scpu` | `scpu.rrd` | % CPU switch (SNMP) |
| `rtemp` | `rtemp.rrd` | Temperatura router en °C (placa y CPU) |
| `macs` | `macs.rrd` | Cantidad de MACs aprendidas |
| `temp` | `temp.rrd` | Temperatura sensor IoT (°C) |
| `hum` | `hum.rrd` | Humedad sensor IoT (%) |
| `viento` | `viento.rrd` | Velocidad del viento (km/h) |
| `presion` | `presion.rrd` | Presión atmosférica (hPa) |
| `rafaga` | `rafaga.rrd` | Ráfaga de viento (km/h) |
| `co2` | `co2.rrd` | Calidad del aire: eCO2 (ppm), TVOC (ppb), AQI (3 DS) |
| `termica` | `termica.rrd` | Tensión (V), corriente (A), potencia (W), energía (kWh), fuga (mA), temp interna (°C) |
| `soil` | `soil.rrd` | Humedad de suelo: 8 DS (lluvia, válvulas, 3 pasto, 3 cantero) |
| `ruptime` | `uptime.rrd` | Días de uptime (reutiliza el RRD y gráfico de `uptime`) |

Los RRDs se actualizan cada vez que el servidor recibe una actualización de estado. Si el archivo RRD no existe, se crea automáticamente al primer dato.

**Períodos disponibles:** 1h, 24h, 7d, 30d, 1y

### Gráfico de ping estilo SmokePing

El gráfico de `ping` implementa el estilo visual de [SmokePing](https://oss.oetiker.ch/smokeping/):

- **10 pings por ciclo** (configurable con `_PING_COUNT` en `ping.py`)
- **RRD schema:** 4 datasources — `mn` (mínimo), `avg` (mediana), `mx` (máximo), `loss` (% pérdida)
- **Banda de humo:** 3 capas de AREA apiladas:
  - Cuartos exterior (claro, `#a8a8a8`) + mitad interior (oscuro, `#606060`) = efecto gradiente
- **Línea de mediana** coloreada según nivel de pérdida:

| Color | Pérdida |
|-------|---------|
| 🟢 Verde `#00cc00` | 0% |
| 🟡 Amarillo `#ffcc00` | 1–10% |
| 🟠 Naranja `#ff8800` | 11–20% |
| 🔴 Rojo oscuro `#cc4400` | 21–50% |
| 🔴 Rojo `#cc0000` | >50% |

- **Leyenda:** mediana, min, max, pérdida promedio y pérdida del último ciclo, más clave de colores de pérdida
- **Migración automática:** si el RRD tiene el schema antiguo (2 o 3 DS), se elimina y recrea automáticamente al primer update con el nuevo schema de 4 DS

### Gráfico de calidad del aire (co2)

El gráfico de `co2` genera **3 paneles apilados** en un único PNG usando Pillow, ya que eCO2, TVOC y AQI tienen escalas incompatibles:

| Panel | DS | Escala | Color |
|-------|----|--------|-------|
| eCO2 | `eco2` | 300–3000 ppm | Azul (AREA) |
| TVOC | `tvoc` | 0–1000 ppb | Verde (LINE) |
| AQI | `aqi` | 0–5 | Naranja (AREA) |

Cada panel tiene su propia escala, unidad y leyenda Max/Min/Avg/Last.

### Gráfico de llaves térmicas (termica)

El gráfico de `termica` genera **3 paneles apilados** en un único PNG (Pillow), ya que potencia, corriente y tensión tienen distintas escalas:

| Panel | DS | Unidad | Color |
|-------|----|--------|-------|
| Potencia | `power` | W | Rojo (AREA) |
| Corriente | `current` | A | Azul (LINE2) |
| Tensión | `voltage` | V (eje 180–250) | Verde (LINE2) |

Cada panel tiene su propia escala, leyenda y unidades. El panel de tensión usa un eje Y fijo (180–250 V) para detectar visualmente variaciones en la red eléctrica.

### Gráfico de sensores de suelo (soil)

El gráfico de `soil` muestra todas las sondas en un único panel con líneas independientes:

| Sensor | DS | Descripción |
|--------|----|-------------|
| PastoSE/NE/NO | `pasto_se/ne/no` | Humedad pasto sur-este, norte-este, norte-oeste |
| CantSur/NE/NO | `cant_sur/ne/no` | Humedad canteros |
| Válvulas | `valv` | Zona de válvulas (valor alto = agua donde no debe haber) |
| Lluvia | `lluvia` | Sensor de lluvia |

Umbrales: válvulas >50% → rojo, 30–50% → amarillo. Suelo <10% → rojo, <20% → amarillo.

### Gráficos ampliados (lightbox)

Al hacer clic en cualquier gráfico (tanto en `/service/` como en `/host/`) se abre un lightbox con el gráfico ampliado (1200×300 px). Se cierra haciendo clic en cualquier lado o presionando `Escape`.

---

## 10. Reconocimientos (ACKs)

Un reconocimiento suprime la visualización de un problema marcándolo como **azul** en vez de rojo/amarillo. El problema sigue siendo monitorado; solo cambia el color visual.

### Crear un ACK

Desde la interfaz web: clic en el servicio → "Reconocer", o desde `/problems` → botón ACK.

**Campos:**
- **Host:** nombre del host
- **Servicios:** nombre del servicio, o patrón regex (`.*` para todos, `all` también funciona — compatibilidad con Perl)
- **Duración:** formato `+Nunit` donde unit es h/d/m/a (horas/días/meses/años), o "Sin vencimiento"
- **Contacto:** email o nombre del responsable
- **Mensaje:** descripción del reconocimiento

**Ejemplos de duración:**

| Input | Significado |
|-------|-------------|
| `+4h` | 4 horas |
| `+2d` | 2 días |
| `+1m` | 1 mes (30 días) |
| `+1a` | 1 año (365 días) |
| `never` | Sin vencimiento |

### Borrar un ACK

Desde `/acks` → botón "Borrar", o desde la vista de host en la tabla de reconocimientos activos.

### Archivos de ACK

Se guardan en `/usr/local/spong/var/database/<hostname>/acks/<id>`. Un ACK vencido se elimina automáticamente al ser leído.

---

## 11. Base de datos

```
/usr/local/spong/var/
├── database/
│   └── <hostname>/
│       ├── services/
│       │   ├── ping-green        ← estado actual (nombre = servicio-color)
│       │   ├── http-red
│       │   └── ...
│       ├── acks/
│       │   └── <id>              ← archivos de reconocimiento activos
│       └── history               ← historial de cambios de estado
├── rrd/
│   └── <hostname>/
│       ├── ping-times.rrd
│       ├── la.rrd
│       ├── mem.rrd
│       └── ...
└── archives/
    └── <hostname>/               ← historial archivado
```

**Formato de archivo de servicio** (`services/ping-green`):
```
timestamp <report_time> <start_time>
<timestamp> <resumen una línea>
    <detalle multilínea...>
```

El nombre del archivo codifica el color actual (`servicio-color`). Cuando el color cambia, el archivo viejo se elimina y se crea uno nuevo.

---

## 12. Logs

| Proceso | Log archivo | Log systemd |
|---------|-------------|-------------|
| spong-server | `/var/log/spong-server.log` | `journalctl -u spong-server` |
| spong-network | `/var/log/spong-network.log` | `journalctl -u spong-network` |
| spong-client | `/var/log/spong-client.log` | `journalctl -u spong-client` |
| spong-web | `/var/log/spong-web.log` | `journalctl -u spong-web` |

Ver logs en tiempo real:
```bash
journalctl -u spong-server -f
# o directamente:
tail -f /var/log/spong-server.log
```

---

## 13. Mantenimiento

### Limpiar servicios stale manualmente

El servidor limpia automáticamente al escanear. Para forzar limpieza de un host:

```bash
ls /usr/local/spong/var/database/<hostname>/services/
rm /usr/local/spong/var/database/<hostname>/services/<servicio>-purple
```

### Agregar un nuevo host

1. Editar `/usr/local/spong/etc/hosts.yaml`:
```yaml
  nuevo-host:
    services: "ping: http ssh"
    contact: "mt"
    ip_addr: ["192.168.0.x"]
```

2. Agregar al grupo en `/usr/local/spong/etc/groups.yaml`

3. Reiniciar `spong-network` para que tome la nueva config

### Agregar un servicio al cliente local

1. Agregar el nombre del plugin a `checks` en `spong.yaml`
2. Agregar el nombre del servicio a `services` del host en `hosts.yaml`
3. Reiniciar `spong-client`

### Archivar historial viejo

El servidor archiva automáticamente según `cleanup.old_history_days` (30 días por defecto). Los archivos de servicio sin actividad por más de `cleanup.old_service_days` (20 días) también se eliminan.

---

## 14. Empaquetado .deb

Los paquetes `.deb` permiten instalar SPONG en cualquier sistema Debian/Ubuntu sin copiar manualmente los archivos.

### Generar los paquetes

```bash
cd /usr/local/spong/packaging
bash build-deb.sh
# Genera:
#   dist/spong-server_3.4.0-1_all.deb  (~53 KB)
#   dist/spong-client_3.4.0-1_all.deb  (~17 KB)
```

### Instalar el servidor

```bash
dpkg -i spong-server_3.4.0-1_all.deb
# Dependencias: python3, python3-pip, rrdtool, fping, iputils-ping
# El postinst:
#   - Crea directorios var/database, var/rrd, var/archives, tmp/
#   - pip install flask werkzeug
#   - Copia *.yaml.example → *.yaml si no existen
#   - systemctl enable/start: spong-server, spong-network, spong-client, spong-web
#   - Opcionalmente configura Apache ProxyPass /spong/
```

### Instalar solo el agente cliente

```bash
dpkg -i spong-client_3.4.0-1_all.deb
# Dependencias: python3
# El postinst es interactivo — pregunta:
#   - Hostname/IP del servidor SPONG
#   - Nombre de este host
#   - Checks a ejecutar (disk diski cpu memory uptime)
# Genera /usr/local/spong/etc/spong.yaml y hosts.yaml
# systemctl enable/start spong-client
```

### Desinstalar

```bash
# Desinstalar (mantiene config y datos):
dpkg -r spong-server
dpkg -r spong-client

# Purga completa (borra var/ y tmp/):
dpkg -P spong-server
dpkg -P spong-client
```

### Estructura del repositorio de empaquetado

```
packaging/
├── build-deb.sh                  # script principal de build
├── dist/                         # paquetes .deb generados
├── spong-server/DEBIAN/
│   ├── control                   # metadatos, dependencias
│   ├── postinst                  # instalación interactiva
│   ├── prerm                     # para servicios antes de desinstalar
│   └── postrm                    # limpieza en purge
└── spong-client/DEBIAN/
    ├── control
    ├── postinst                  # pregunta server/host/checks
    ├── prerm
    └── postrm
```

---

## 15. GitHub Actions (CI)

El archivo `.github/workflows/build-deb.yml` automatiza la construcción de los paquetes `.deb` en cada push.

### Cuándo se ejecuta

| Evento | Qué hace |
|--------|----------|
| Push a `main` | Construye los `.deb` y los sube como artefacto del workflow (disponibles 30 días) |
| Pull Request a `main` | Verifica que el build no se rompe |
| Tag `v*` (ej: `v3.2`) | Build + crea un **GitHub Release** con los `.deb` adjuntos |

### Crear una release oficial

```bash
git tag v3.1
git push origin v3.1
# GitHub Actions construye y publica la release automáticamente
```

### Descargar artefactos de un build

En GitHub → pestaña **Actions** → seleccionar el workflow → sección **Artifacts** → `spong-deb-<sha>`.

---

## 16. Historial de cambios

### v3.4.0 — 2026-04

**Feat: network agent — chequeo en bloques configurables**
- `network_agent.py`: `_check_hosts_parallel` ahora procesa los hosts en lotes sucesivos en lugar de lanzar todos a la vez; cada bloque termina antes de iniciar el siguiente, evitando la saturación de la máquina cuando hay muchos hosts
- Nuevo parámetro de configuración `network.batch_size` (default: `20`): cantidad de hosts por bloque
- `network.workers` sigue controlando el número máximo de hilos dentro de cada bloque
- Ejemplo de config:
  ```yaml
  network:
    batch_size: 20   # hosts por lote
    workers: 30      # hilos máximos dentro del lote
  ```

**Feat: sensores temp/hum — nuevos sensores HTTP**
- `temp.py` y `hum.py`: sensor `living` renombrado de `esp1s-sensor-temperatura` a `sensor-temp-living`
- Agregados sensores `pieza-chica` y `pieza-ninias` vía HTTP (antes `pieza-ninias` leía de archivo local)
- Umbrales `pieza-chica` agregados a `_THRESHOLDS` en `temp.py`

**Versión bumpeada a 3.4.0**
- `spong/__init__.py`: `3.4.0`
- Paquetes: `spong-server_3.4.0-1_all.deb`, `spong-client_3.4.0-1_all.deb`

---

### v3.3.2 — 2026-04

**Fix: tema oscuro por defecto**
- La interfaz web ahora inicia en modo oscuro para usuarios sin cookie de preferencia de tema
- El modo claro sigue disponible con el botón ☀ en el header

**Fix: instalador .deb — dependencias Python**
- Reemplazado `python3-pip` / `python3-wheel` en `Depends` por paquetes apt nativos: `python3-flask`, `python3-werkzeug`, `python3-yaml`
- Elimina el error de instalación en Debian 12+ / Ubuntu 24.04 donde `python3-wheel` no existe como paquete separado
- El `postinst` ahora solo usa `pip3` para instalar `tinytuya` (único paquete sin equivalente apt), con soporte para `--break-system-packages` (PEP 668)

**Fix: instalador .deb — message.yaml faltante**
- `build-deb.sh` ahora incluye `message.yaml.example` en el paquete
- `postinst` copia `message.yaml.example` → `message.yaml` si no existe, evitando el error de arranque del servidor

---

### v3.3.1 — 2026-04

**Fix: scpu — soporte Cisco SG550X + SwOS**
- `scpu.py` ahora prueba el OID Cisco SG500/SG550 (`1.3.6.1.4.1.9.6.1.101.1.7.0`) antes que TP-Link y HR CPU
- Si ningún OID responde y el `sysDescr` contiene `SwOS`, retorna `clear` (SwOS no soporta CPU via SNMP)
- Corrección de dependencia: `snmp_get_str` se agregó a `snmp.py` en el mismo cambio que `scpu.py` lo importaba; el agente de red debía reiniciarse para cargar la versión nueva

**Fix: rtemp — fallback SwOS**
- Agrega OID alternativo MikroTik SwOS para temperatura cuando los OIDs RouterOS no responden

**Fix: NTP — detección de formato moderno**
- `ntp.py` detecta el formato de salida moderno de ntpdate (`+X.XXXXXX +/- Y.YYYYYY ... sN`)
- El summary ahora incluye el offset: `ntp ok offset -0.003s`

**Fix: HTTP — resolución via config**
- `http.py` usa la IP configurada en Spong (no DNS) cuando el `hname` coincide con el hostname monitoreado; evita fallos en switches/routers con nombres no resolvibles

**Nuevos plugins de red**
- `wassoc.py`: clientes WiFi asociados (AP via SNMP)
- `wuptime.py`: uptime de AP via SNMP
- `poppassd.py`: chequeo de servicio poppassd
- `scpu1m.py`, `scpu5s.py`: CPU switch promedio 1 min / 5 seg
- `freq_in.py`, `freq_out.py`, `volt_in.py`, `volt_out.py`, `temp_bat.py`, `temp_ext.py`: métricas UPS extendidas

**RRD — nuevos gráficos**
- `wassoc`: gráfico de clientes WiFi asociados
- `scpu1m`, `scpu5s`: gráficos de CPU switch en ventanas 1m/5s

**Refactor interno**
- `snmp.py`: helper `_snmp_get_raw()` compartido entre `snmp_get_int` y `snmp_get_str`; socket manejado con context manager (sin leaks)
- `rrd.py`: helper `_update_count_rrd()` compartido entre `_update_macs` y `_update_wassoc`
- Dependencia `rpcbind` agregada al paquete server (requerida por NFS check)

**Versión bumpeada a 3.3.1**
- `spong/__init__.py`: `3.3.1`
- Paquetes: `spong-server_3.4.0-1_all.deb`, `spong-client_3.4.0-1_all.deb`

### v3.3 — 2026-04

**Gráficos RRD — leyenda con estadísticas**
- Todos los gráficos de speedtest muestran Máx/Mín/Prom/Últ en la leyenda (via `GPRINT`)
- Fix: fondo transparente en gráficos apilados (speedtest, UPS) → ahora fondo blanco sólido
- Fix: sintaxis `AREA:band#color::` inválida en rrdtool → corregida
- Altura mínima por sub-panel: `max(height//2-10, 120)` para evitar gráficos aplastados

**Speedtest — gráfico estilo SmokePing**
- Panel de latencia rediseñado: banda semitransparente `ping ± jitter` (smoke)
- DS `jitter` agregado al RRD `speedtest.rrd` (4 DS: down, up, ping, jitter)
- El summary del plugin ahora incluye `jitter:X.Xms` para persistir en RRD
- Bordes de la banda con líneas semi-opacas (estilo SmokePing)
- Leyenda separada para ping y jitter con sus propias estadísticas

**Speedtest — intervalo ajustado**
- `interval: 280s` (< sleep del cliente 300s) → corre en cada ciclo del cliente
- `sleep spong-client: 300s` (antes 500s) → ciclo cada ~5 minutos
- Heartbeat RRD: 750s (2.5 × 300s)

**Gráficos TCP — percentiles P50/P90/P95**
- Nuevo helper `_tcp_time_graph_args()`: todos los gráficos de tiempo de respuesta (SSH, HTTP, HTTPS, MySQL, DNS, Telnet, FTP, SMTP, IMAP, NTP, RTSP) muestran líneas horizontales P50/P90/P95 con sus valores en la leyenda

**Speedtest — gráfico comparativo de períodos**
- Nueva función `_graph_speedtest_compare()`: 3 paneles (bajada/subida/ping) superponiendo semana actual, semana anterior y hace 1 mes
- Nuevo panel "Comparar períodos" en la página de servicio speedtest

**UI — versión dinámica**
- El tooltip del logo ya no tiene la versión hardcodeada; se lee de `spong/__init__.py` en tiempo de ejecución
- Keys de traducción de i18n separadas de la versión (antes `"SPONG v3.1 — creado por mt"`, ahora `"creado por mt"`)

**Versión bumpeada a 3.3**
- `spong/__init__.py`: `3.3.0`
- Paquetes: `spong-server_3.4.0-1_all.deb`, `spong-client_3.4.0-1_all.deb`

### v3.2 — 2026-04

**Plugin speedtest (cliente)**
- Nuevo plugin cliente `speedtest.py`: mide bajada, subida y latencia con el CLI de Ookla
- Umbrales: <5 Mbps rojo, <10 Mbps amarillo (configurable en `thresholds.speedtest` de `spong.yaml`)
- Opción `server_id` en `thresholds.speedtest` para fijar el servidor Ookla a usar
- Flags `--accept-license --accept-gdpr` para correr sin TTY desde systemd
- Fix: `HOME=/root` en el entorno del subprocess (el servicio no hereda HOME)
- Gráficos RRD con dos paneles: bajada/subida (Mbps) y ping/jitter (ms)
- Los plugins cliente se registran en `checks:` de `spong.yaml`, no en `hosts.yaml`

**Sensor HTTP (temp/hum)**
- Plugins `temp.py` y `hum.py` ahora soportan sensores HTTP con `_HTTP_MAP` y `_http_read()`
- Nuevo host `living` en grupo `clima` leyendo `temperature_c` y `humidity_pct` de `http://esp1s-sensor-temperatura/json`

**Mejoras de UI**
- Página de servicio: botón **Borrar reconocimiento** cuando el servicio está reconocido
- Fix: el botón usaba el filename del ack en vez del formato `host-services-endtime` que espera el protocolo

**`client_agent.py`**
- Eliminado hostname `s2` hardcodeado; ahora usa `hostname:` de `spong.yaml` o `socket.gethostname()` como fallback

**Empaquetado .deb**
- `postinst` instala dependencias del sistema (`rrdtool`, `fping`, `snmp`) via `apt-get install -y` para que funcione con `dpkg -i` directo
- Agregado `pyyaml` al `pip3 install` del postinst

**Fix speedtest — intervalo mínimo entre mediciones**
- El plugin ahora verifica el timestamp del último resultado antes de correr
- Si la última medición fue hace menos de `interval` segundos (default: 3600), se saltea
- Configurable con `thresholds.speedtest.interval` en `spong.yaml`
- Heartbeat del RRD aumentado de 7200 → 9000s (2.5× intervalo) para evitar cortes en gráficos

**Versión bumpeada a 3.2**
- `spong/__init__.py`: `3.2.0`
- Paquetes: `spong-server_3.4.0-1_all.deb`, `spong-client_3.4.0-1_all.deb`

### v3.1 — 2026-03 (parte 10)

**Plugin speedtest (cliente)**

- `speedtest.py` — mide bajada, subida y latencia via Ookla speedtest CLI. Plugin de cliente: corre en el host monitoreado, no requiere conectividad entrante
- Umbrales configurables en `spong.yaml` bajo `thresholds.speedtest` (down_warn/crit, up_warn/crit, ping_warn/crit)
- RRD con 2 paneles apilados: velocidad (down AREA azul + up LINE verde) y latencia (ping LINE naranja)
- Heartbeat 2h para tolerar tests infrecuentes

Uso en `hosts.yaml`:
```yaml
mi-host:
  services: "disk cpu memory speedtest"
```

Umbrales opcionales en `spong.yaml`:
```yaml
thresholds:
  speedtest:
    down_warn: 10    # Mbps
    down_crit:  5
    up_warn:    5
    up_crit:    2
    ping_warn: 50    # ms
    ping_crit: 100
```

**Fix ícono 📊**

- El ícono de gráfico en la lista de servicios del host ahora solo se muestra si existe RRD para ese servicio (HEAD request al endpoint). Servicios sin gráfico (snmp, nfs, interfaces, etc.) ya no muestran el ícono

### v3.1 — 2026-03 (parte 9)

**Nuevos plugins (port desde Perl)**

- `ups.py` — UPS APC via SNMP (PowerNet MIB): tensión entrada/salida, frecuencia entrada/salida, temperatura batería y exterior (sonda opcional). RRD con 2 paneles apilados (tensión + frecuencia). Umbrales para red Argentina 220V/50Hz
- `interfaces.py` — interfaces de red caídas via SNMP IF-MIB: detecta interfaces admin up / oper down. Lista configurable de interfaces a ignorar (`ignore_interfaces` en hosts.yaml)
- `nfs.py` — disponibilidad NFS via `rpcinfo -p`: verifica nfsd (100003) y mountd (100005)
- `memolt.py` — uso de memoria % en switches/routers TP-Link via TPLINK-SYSMONITOR-MIB (OID verificado en T2600G). RRD gráfico AREA violeta. Uso: `services: "snmp scpu memolt"`

**Fix presence plugin**

- Sensor Tuya a veces no incluye DPS 102 (lux) cuando no hay presencia → cortes en gráfico. Ahora se reutiliza el último valor de lux conocido por hostname

### v3.1 — 2026-03 (parte 8)

**Interfaz mobile responsive**

- Sidebar oculta por defecto en pantallas ≤700px; se abre con botón hamburger (☰) y overlay táctil para cerrar
- Header compacto: nav colapsada (links en sidebar), reloj oculto en mobile
- Matrix table y gráficos RRD con scroll horizontal táctil (`-webkit-overflow-scrolling: touch`)

**Plugin presence: sensor de presencia humana Tuya (mmWave)**

- `presence.py` — lee estado de presencia, distancia (cm) y luminosidad (lux) via tinytuya protocolo local
- Configurable en `etc/sensors.yaml` (gitignoreado). Se incluye `etc/sensors.yaml.example`
- Estados: `none` → clear, `peaceful` → green, `move/large_move/small_move` → yellow
- RRD: 3 DS (state 0/1/2, dist cm, lux), paso 60s; gráfico de 2 paneles apilados (lux AREA naranja + distancia LINE azul)

**Gráficos RRD para RTSP y mejoras**

- `rtsp` añadido a los dispatchers de RRD: guarda tiempo de respuesta en `rtsp-time.rrd`, color cian en gráfico
- Verificación on-demand de servicio (clic en badge de estado en página `/service/HOST/SVC`) actualiza badge, resumen, timestamp y mensaje sin recargar
- `camara-tapo-garaje` añadida con servicio `rtsp`

### v3.1 — 2026-03 (parte 7)

**Nuevos plugins de red**

- `rtsp.py` — disponibilidad de cámaras: prueba RTSP/554 con OPTIONS estándar; si falla, fallback a Tapo/2020 (protocolo propietario C-series). Cámaras Tapo responden en ambos puertos
- `soil.py` — sensores de humedad de suelo via SSH JSON (riegopi): pasto (3 zonas), canteros (3 zonas), sensor de lluvia y válvulas. Lógica de válvulas invertida: valor alto = agua presente = alarma (zona donde no debería haber agua)
- `ruptime.py` — uptime via SSH sin necesitar spong-client instalado, con caché de 55s. Reutiliza RRD y gráfico de `uptime`. Timeout de 30s para hosts lentos
- RRD: `_update_soil()` con 8 DS + `_graph_soil()` (panel único multi-línea)

**Fixes**

- `soil.py`: umbrales corregidos — sensores de suelo reportan % de humedad (100% = muy húmedo = bien); válvulas: <30% verde, 30–50% amarillo, >50% rojo
- `rtsp`: cámaras sin soporte RTSP removidas de `hosts.yaml` (camara-oficina, camara-living)
- `ruptime`: ConnectTimeout aumentado a 30s para hosts SSH lentos

### v3.1 — 2026-03 (parte 6)

**Plugin termica: llaves térmicas Tuya**

- `termica.py` — plugin de red que lee tensión, corriente, potencia, energía, corriente de fuga y temperatura interna directamente via `tinytuya` (protocolo local, sin cloud)
- Configuración en `etc/termicas.yaml` (gitignoreado — contiene claves locales Tuya). Se incluye `etc/termicas.yaml.example` como plantilla
- Caché interno de 55 s por dispositivo para no saturar los dispositivos Tuya en cada ciclo de checks
- Recarga automática de config al detectar cambio de mtime en `termicas.yaml`
- Soporte para firmware 3.3, 3.4 y 3.5 con decodificación de payload binario DPS17

**Gráfico termica con 3 paneles apilados**

- `rrd.py` actualizado con `_update_termica()` (6 DS: voltage/current/power/energy/leakage/temp) y `_graph_termica_stacked()`
- `_graph_termica_stacked()` genera 3 paneles independientes apilados con Pillow: Potencia (rojo AREA), Corriente (azul LINE), Tensión (verde LINE, eje fijo 180–250 V)
- Migración de datos históricos desde proyecto externo via `rrdtool dump | rrdtool restore`

**Fix ACK "Sin vencimiento"**

- Al marcar el checkbox "Sin vencimiento" en el formulario ACK, el campo de duración quedaba `disabled` y no se enviaba en el POST — el servidor interpretaba duración vacía como 4 horas
- Fix: el submit handler re-habilita el input antes de enviar el formulario

**Fix botón ACK en página de reconocidos**

- El botón de reconocer en `/acks` mostraba "ACK" en vez del texto traducido
- Fix: cambiado texto hardcodeado por `{{ _('Reconocer') }}`

**Seguridad**

- `etc/termicas.yaml` agregado a `.gitignore` para no subir claves Tuya al repo
- El paquete `.deb` incluye `termicas.yaml.example` pero nunca `termicas.yaml`
- `postinst` actualizado: instala `tinytuya` junto con `flask` y `werkzeug`

### v3.1 — 2026-03 (parte 5)

**Sensores IoT via SSH JSON y host virtual de clima**

- `_ssh_json.py` — helper con caché de 60s: hace `ssh root@host cat /path/file.json` sin dependencias extra
- `temp.py` y `hum.py` extendidos con `_SSH_MAP` para leer sensores de hosts remotos
- `co2.py` — nuevo plugin para calidad del aire: parsea eCO2 (ppm), TVOC (ppb) y AQI desde JSON via SSH
- Patrón host virtual: `riego-patio` (mismo IP que `riegopi`) aparece en grupo **Clima** con servicios `temp hum co2`; `riegopi` se mantiene en **Servers** con `ping: ssh`
- Eliminados plugins redundantes `ptemp.py`, `phum.py`, `pco2.py` — consolidado en `temp`/`hum`/`co2`

**Gráfico co2 con 3 paneles apilados**

- `rrd.py` actualizado con `_update_co2()` (3 DS: eco2, tvoc, aqi) y `_graph_co2_stacked()`
- `_graph_co2_stacked()` genera 3 sub-gráficos rrdtool independientes y los apila verticalmente con Pillow
- Cada panel tiene su propia escala y unidad: evita la confusión de mezclar ppm/ppb/AQI en un solo eje

### v3.1 — 2026-03 (parte 4)

**GitHub Actions (CI/CD)**
- `.github/workflows/build-deb.yml` — build automático en cada push a `main`
- En push: construye `.deb` y los sube como artefacto (30 días de retención)
- En tag `v*`: construye y crea un **GitHub Release** con los `.deb` adjuntos
- Badge de estado del build en el README

**Documentación**
- README: encabezado en inglés con feature list y badge de CI
- Sección **Instalación rápida** (quickstart de 5 pasos) visible antes del índice
- Sección **GitHub Actions** con instrucciones para crear releases oficiales
- Sección de migración desde Perl incluida en el quickstart

### v3.1 — 2026-03 (parte 3)

**Script de migración desde SPONG Perl**
- `bin/spong-migrate.py` — convierte los archivos de configuración del SPONG original (Perl) al formato YAML v3
- Soporta `spong.conf` → `spong.yaml`, `spong.hosts` → `hosts.yaml`, `spong.groups` → `groups.yaml`
- Parser regex puro sin dependencias externas ni Perl instalado
- Maneja comentarios Perl, hashes anidados, arrays `[...]`, entradas comentadas
- Modo `--all` detecta automáticamente los archivos en el directorio actual

```bash
# Uso básico — detecta todo en el directorio actual
python3 /usr/local/spong/bin/spong-migrate.py --all

# Archivos específicos
python3 /usr/local/spong/bin/spong-migrate.py \
  --conf spong.conf --hosts spong.hosts --groups spong.groups \
  --outdir /usr/local/spong/etc/ --force
```

### v3.1 — 2026-03 (parte 2)

**Check on-demand al presionar el badge de estado**
- Al hacer clic en el badge de color de un servicio en `/host/<name>`, se ejecuta el plugin de red correspondiente en tiempo real
- El plugin corre en un `ThreadPoolExecutor` con timeout de 35s para no bloquear el servidor web
- Si el servicio no tiene plugin de red (disk, cpu, etc.), hace un GET de solo lectura del estado actual
- Después del check: actualiza badge, resumen y celda "Último reporte" en la tabla sin recargar la página
- El sidebar izquierdo también se actualiza: si el servicio resuelve (pasa a green/clear/blue), se elimina del panel de problemas y actualiza el contador del grupo

**Capturas de pantalla en el README**
- Screenshots automáticos con Playwright (Chromium headless) en `docs/screenshots/`
- Cuatro capturas: vista de grupos, host con servicios, página de problemas, modo oscuro

### v3.1 — 2026-03

**Interfaz web multi-idioma**
- Selector de idioma en el header: menú desplegable con bandera y nombre nativo
- 7 idiomas: Español (default), English, Français, Deutsch, Português, 中文, Русский
- Traducción completa de la UI: navegación, tablas, botones, mensajes, tiempos relativos
- Cookie permanente: se renueva automáticamente en cada visita, duración 10 años
- Sin dependencias externas — sistema de traducción propio en `app.py`

**Fix cookie de idioma**
- `after_request` sobreescribía la cookie nueva con el valor viejo al cambiar idioma
- Corregido: el hook omite la respuesta cuando el endpoint es `set_lang`

**Dark mode**
- Botón 🌙/☀ en el header alterna entre modo claro y oscuro
- Implementado con variables CSS (`:root` / `html.dark`) — sin JS, sin flash al cargar
- Cubre toda la UI: fondo, cards, tablas, sidebar, formularios, filas de estado
- Cookie permanente igual que el idioma (10 años, se renueva en cada visita)

**Versión bumpeada a 3.1**
- Tooltip del logo actualizado a v3.1 en todos los idiomas
- Paquetes .deb: `spong-server_3.4.0-1_all.deb` (53 KB), `spong-client_3.4.0-1_all.deb` (17 KB)

### v3.0 — 2026-03 (parte 3)

**Empaquetado .deb**
- Creados `packaging/build-deb.sh`, `spong-server/DEBIAN/` y `spong-client/DEBIAN/`
- `spong-server`: incluye todo (server + network + client + web), postinst instala dependencias pip y habilita 4 servicios systemd
- `spong-client`: solo el agente, postinst interactivo pregunta servidor/hostname/checks
- Paquetes generados: `spong-server_3.4.0-1_all.deb` (49 KB) y `spong-client_3.4.0-1_all.deb` (17 KB)

### v3.0 — 2026-03 (parte 2)

**Proxy Apache en `/spong`**
- Configurado `ProxyPass /spong/ → localhost:8090` en `000-default.conf` y `default-ssl.conf`
- Habilitados módulos `proxy`, `proxy_http`, `headers` en Apache
- Agregado `ProxyFix` middleware en Flask (`app.py`) para leer `X-Forwarded-Prefix` y ajustar `SCRIPT_NAME`
- Reemplazados todos los links hardcodeados (`href="/"`, `href="/host/..."`, etc.) en los 6 templates por `url_for()` — funciona tanto en acceso directo `:8090` como vía Apache `/spong`
- Reemplazada config antigua del Perl spong (`Alias /spong /usr/local/spong/www/`) con el nuevo ProxyPass

**Servicios TCP con gráficos RRD**
- Agregados `telnet`, `ftp`, `smtp`, `imap`, `ntp` a `update_from_status` y `graph_png` en `rrd.py`
- Mismo mecanismo que `ssh`/`dns`: parsea `"Xs"` del summary, guarda en `<svc>-time.rrd`
- Colores diferenciados por servicio: telnet=naranja, ftp=azul, smtp=verde, imap=violeta, ntp=teal

**Arranque automático con systemd**
- Creados 4 unit files en `/etc/systemd/system/spong-{server,network,client,web}.service`
- Todos habilitados con `systemctl enable` — arrancan automáticamente en cada boot
- `spong-network`, `spong-client` y `spong-web` requieren `spong-server` (dependencia `Requires=`)
- Logs en `/var/log/spong-*.log` y via `journalctl -u spong-*`
- `Restart=on-failure` en todos los servicios

### 2026-03 (sesión anterior)

**Gráfico de ping estilo SmokePing**
- `ping.py` ahora envía 10 pings por ciclo y parsea min/avg/max/pérdida del output de `ping`
- Nuevo schema de `ping-times.rrd`: 4 DS (`mn`, `avg`, `mx`, `loss`) en lugar de los 2-3 anteriores
- Gráfico con banda de humo gris graduada (3 capas AREA apiladas) y línea de mediana que cambia de verde a rojo según la pérdida de paquetes — visualmente idéntico a SmokePing
- Los RRDs existentes con el schema viejo se migran automáticamente al primer update

**Fix falso rojo en rtemp (RBcAPGi)**
- El modelo RBcAPGi-5acD2nD hace round-robin entre sensores internos en el OID `.14.0`, alternando entre ~44°C, ~67°C y ~89°C en consultas sucesivas
- `rtemp.py` ahora consulta el OID 3 veces y toma el valor mínimo, eliminando los falsos alarmas rojos

**Fix schema rtemp.rrd**
- Los RRDs de `rtemp` creados por una versión anterior tenían un DS llamado `temp` en vez de `board`/`cpu`
- El mismatch causaba que todas las actualizaciones escribieran `U` (desconocido)
- Solución: borrar el archivo; se recrea automáticamente con el schema correcto al siguiente ciclo

**Corrección general de reinicio de procesos**
- `spong/rrd.py` es importado por el servidor. Los cambios en `rrd.py` requieren reiniciar `spong-server` (no solo `spong-network`) para tomar efecto en las actualizaciones de RRD

### 2026-02/03 (sesión anterior)

- HTTPS: soporte TLS legacy y fallback TCP para equipos con certificados RSA-1024 débiles
- Gráficos de tiempo de respuesta: http, https, ssh, dns, mysql
- Lightbox para ampliar gráficos (clic → 1200×300 px, cerrar con Escape)
- Plugins SNMP reescritos para obtener datos reales: rcpu, scpu, rtemp, macs
- SNMPv1 implementado desde cero (sin librerías externas): GET y GETNEXT, parseo ASN.1 TLV
- `check_snmp` muestra sysDescr como texto legible en vez de hex crudo
- Gráficos RRD para: rcpu, scpu, rtemp, macs, uptime, dns, sensores IoT (temp, hum, viento, presion, rafaga)
- Leyendas Min/Max/Avg/Last en todos los gráficos
- Sidebar muestra "✓ Sin problemas" cuando no hay servicios rojos
- Eliminado enlace API del header y sidebar

---

## Estructura de directorios

```
/usr/local/spong/
├── bin/                    ← ejecutables
│   ├── spong-server
│   ├── spong-network
│   ├── spong-client
│   └── ...
├── etc/                    ← configuración
│   ├── spong.yaml
│   ├── hosts.yaml
│   ├── groups.yaml
│   └── message.yaml
├── spong/                  ← código Python
│   ├── server.py
│   ├── network_agent.py
│   ├── client_agent.py
│   ├── database.py
│   ├── models.py
│   ├── config.py
│   ├── rrd.py
│   ├── protocol.py
│   └── plugins/
│       ├── client/         ← plugins del agente local
│       └── network/        ← plugins del agente de red
├── web/                    ← interfaz web
│   ├── app.py
│   └── templates/
└── var/                    ← datos en tiempo de ejecución
    ├── database/
    ├── rrd/
    └── archives/
```

---

## Licencia

SPONG v3 — Copyright (C) 2026 mt

Este programa es software libre: podés redistribuirlo y/o modificarlo bajo los términos de la
[GNU General Public License v3](LICENSE) publicada por la Free Software Foundation.

Este programa se distribuye con la esperanza de que sea útil, pero **sin ninguna garantía**.
Ver el archivo [`LICENSE`](LICENSE) para más detalles.
