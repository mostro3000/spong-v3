# Troubleshooting de clientes SPONG — guía operativa

Guía basada en los problemas reales encontrados al desplegar `spong-client`
en la flota (jun–ago 2026). Servidor de monitoreo en producción: **s3vz**
(192.168.96.26). El repo fuente y el build de los `.deb` viven en **s2**
(`/usr/local/spong`).

## 1. Instalación correcta de un cliente

```bash
# apt resuelve las dependencias (python3-yaml, etc.); dpkg -i NO lo hace
apt install ./spong-client_<version>_all.deb

# En la config interactiva del postinst:
#   Servidor SPONG:  s3vz        <-- NO "s3", NO "s3.unsl.edu.ar" (ver §3.1)
#   Nombre del host: <hostname>  (default correcto)
#   Checks estándar de la flota:
#     disk diski cpu memory uptime sensors logs chronyc disktemp
```

Verificación (siempre, desde el server):

```bash
ls -la --time-style=+%H:%M:%S /usr/local/spong/var/database/<host>/services/
# Deben aparecer archivos <check>-green con mtime de hace segundos.
```

## 2. Diagnóstico rápido: "instalé el cliente y no llega nada"

Síntoma típico en la web: el host aparece con **todos los checks de cliente
en purple** ("No data received") pero `ping`/`ssh` verdes (esos los hace el
server). Chequear en el cliente, en orden:

```bash
systemctl is-active spong-client        # ¿corre?
cd /usr/local/spong && timeout 10 python3 bin/spong-client --nodaemonize 2>&1 | head
                                        # <-- el traceback/error real sale acá
grep -A1 '^server:' etc/spong.yaml      # ¿apunta a s3vz?
timeout 3 bash -c 'echo > /dev/tcp/s3vz/1998' && echo OK   # ¿llega al puerto?
```

## 3. Fallas conocidas (todas vistas en producción)

### 3.1 Server equivocado: `s3.unsl.edu.ar` (i25 y i14, ago 2026)
- **Síntoma**: cliente `active`, log lleno de
  `status_sender: s3.unsl.edu.ar:1998 - [Errno 111] Connection refused`.
- **Causa**: en el postinst se respondió `s3.unsl.edu.ar` (resuelve a
  190.122.236.26 = pr.unsl.edu.ar, otra máquina). El server real es `s3vz`.
- **Fix**: `sed -i 's/s3.unsl.edu.ar/s3vz/' etc/spong.yaml && systemctl restart spong-client`.
- **Barrido para detectar más víctimas** (en s3vz): hosts con client-checks
  purple y ping verde:
  ```bash
  cd /usr/local/spong/var/database
  for h in */; do h=${h%/}; [ -f "$h/services/ping-green" ] && \
    ls "$h"/services/cpu-purple >/dev/null 2>&1 && echo "$h"; done
  ```

### 3.2 `ModuleNotFoundError: No module named 'yaml'` (webmailvz, jul 2026)
- Debian 13 mínimo no trae `python3-yaml` y los `.deb` < 3.6.5 no lo
  declaraban en `Depends`. Fix: `apt install python3-yaml` (o usar deb ≥ 3.6.5
  instalado con `apt install ./...`).

### 3.3 `Unit spong-server.service not found` (i57, jul 2026)
- Los `.deb` ≤ 3.6.2 empaquetaban la unit del build-host (servidor) con
  `Requires=spong-server.service`. En un host solo-cliente systemd rechaza el
  arranque. Corregido en 3.6.3 (unit propia del cliente). Fix manual: sacar
  `Requires=`/`After=...spong-server` de la unit + `daemon-reload`.

### 3.4 `FileNotFoundError: etc/message.yaml` (o `groups.yaml`)
- `config.load_all()` abre siempre esos YAML; los `.deb` ≤ 3.6.2 no los
  creaban. Corregido en 3.6.3 (postinst los genera vacíos). Fix manual:
  `printf 'message: {}\n' > etc/message.yaml` (ídem `groups: {}`).

## 4. Limpieza de servicios residuales (purple viejos)

Cuando un host cambia de cliente (Perl viejo → Python) quedan en la base del
server servicios que ya nadie manda (`jobs`, `hddtemp`, `zfs`, `drbd`, ...)
clavados en purple. Antes de borrar:

1. **Verificar que el servicio realmente no corresponde** (¿el host tiene
   ZFS/DRBD?). Si corresponde, lo que falta es el plugin en el cliente nuevo,
   no borrar el aviso.
2. **Backup** y borrado:
   ```bash
   cd /usr/local/spong/var/database/<host>
   tar czf services-bak-$(date +%Y%m%d).tar.gz services/
   rm services/<svc>-purple ...
   ```

Los checks del mismo nombre que el cliente nuevo sí manda (cpu, disk, ...)
no hace falta borrarlos: el próximo status verde reemplaza al purple.

## 5. Hora en la web ("el historial está corrido")

- Todos los timestamps (historial incluido) se renderizan **server-side** con
  el localtime del server. Desde v3.7.5 el reloj del header y (a07e4ee) el
  refresh AJAX del servicio también muestran la hora **del server**, no la del
  navegador — un navegador en otra TZ ya no puede "contradecir" al historial.
- Si aún así las horas están corridas: revisar la TZ **del sistema** del
  server (`timedatectl`). wifivz estaba en UTC (jul 2026); se corrigió con
  `timedatectl set-timezone America/Argentina/Buenos_Aires`.
- Ojo: los procesos cachean la TZ — tras cambiarla hay que reiniciar
  `spong-web` (y opcionalmente server/network para sus logs).

## 6. Nota sobre versiones desplegadas

Las instancias desplegadas (s3vz, wifivz) suelen correr código **anterior** al
HEAD de s2, más hotfixes aplicados en caliente. Antes de diagnosticar un bug
"del server", conviene revisar si s2 ya lo arregló en un release posterior
(`git log -S <símbolo>` en s2) y considerar actualizar la instancia con el
`.deb` actual.

## 7. Clientes legacy (Perl) — p. ej. i19

Algunos hosts viejos siguen corriendo el spong-client **Perl original** (no el
cliente Python de este repo). Cómo reconocerlos y operarlos:

- **Identificación**: no hay unit systemd ni paquete `.deb`; el proceso es
  `spong-client (sleeping)` (Perl) y el código vive en
  `/usr/local/spong/bin/spong-client` + `/usr/local/spong/lib/Spong/`.
- **Config**: `/etc/spong/spong.conf`. Los checks activos están en la línea
  `$CHECKS = 'disk diski cpu ... chronyc btrfs';`.
- **Plugins**: `/usr/local/spong/lib/Spong/Client/plugins/check_<nombre>`.
  Se cargan todos al iniciar; corre solo lo listado en `$CHECKS`. El plugin
  registra `$CHECKFUNCS{'<nombre>'} = \&check_<nombre>;` y reporta con
  `status($SPONGSERVER, $HOST, "<nombre>", $color, $summary, $message)`.
- **Validar sintaxis** (el `use Spong::SafeExec` necesita el include path):
  `perl -I/usr/local/spong/lib -c .../plugins/check_<nombre>`
- **Reiniciar / recargar**: `kill -HUP <pid>` — el cliente se re-ejecuta a sí
  mismo releyendo config y plugins (también acepta USR1; QUIT lo termina).
- **Paths**: los hosts legacy pueden NO estar usr-mergeados (`/bin` real, no
  symlink) — usar `/bin/btrfs`, `/bin/findmnt`, etc. en los plugins: esos
  paths funcionan también en sistemas modernos usr-mergeados.
- **Copia maestra del árbol Perl**: en s2 `/usr/local/spong/lib/` (está en
  `.gitignore`, no es parte del repo v3). Plugins Perl custom existentes:
  `check_btrfs` (s2 + i19, equivalente al `btrfs.py` de v3.7.7) y
  `check_chronyc` (solo i19; el equivalente v3 es `chronyc.py` desde 3.6.4).
- **Migración**: para pasar un host legacy al cliente nuevo, instalar el
  `spong-client_*.deb` actual (ver §1) y replicar en `checks:` lo que tenía en
  `$CHECKS` (ojo con checks sin equivalente directo, p. ej. `processes`).

## 8. Plugin `claude` (estado de Claude Code) — desde v3.7.8

Check de cliente que responde, **sin gastar tokens ni cuota**, si Claude
Code está instalado, si hay que rehacer login, si la suscripción está al día
y cuánto de la cuota (5 h / semana) va usada. Pensado para cuentas Pro/Max
con login de claude.ai (no usa API key). Detalle y umbrales en README §5.

### 8.1 Dónde está desplegado (sep 2026)

| Host | Spong | Cómo | Cuenta |
|------|-------|------|--------|
| s2 | repo `main` corriendo en vivo (`/usr/local/spong`) | plugin bundled (`spong/plugins/client/claude.py`) | root, Max |
| mmg1.esc10sl.edu.ar | **servidor propio** `spong-server 3.5.11-1` (`.deb`); se monitorea a sí mismo, NO reporta a s3vz | override `etc/plugins/client/claude.py` (copiado el 2026-09-15, igual al de `fbda7a8`) | root, Max |
| s3vz / resto de la flota | — | no instalado | — |

mmg1 tiene su propia web (puerto 8090) y su propio `hosts.yaml`; el servicio
`claude` se agregó ahí, no en s2. Backups de sus yaml previos en
`var/config_history/manual-20260915-155241/` (en mmg1).

### 8.2 Instalar en otro host sin actualizar el `.deb`

Funciona con cualquier spong ≥ 3.5.x (necesita `plugin_loader` con dir de
overrides y `config.get_threshold`) y Python ≥ 3.9 (con el fix `fbda7a8`;
el `claude.py` del tag v3.7.8 pide 3.10).

```bash
# desde s2
scp spong/plugins/client/claude.py root@HOST:/usr/local/spong/etc/plugins/client/claude.py
ssh root@HOST
  cd /usr/local/spong
  # spong.yaml: agregar claude a checks y el bloque thresholds.claude
  #   checks: "... claude"
  #   thresholds:
  #     claude:
  #       users: root          # usuario con ~/.claude/.credentials.json
  #       usage_warn: 80
  #       usage_crit: 100
  #       refresh_warn_days: 3
  #       interval: 600
  # hosts.yaml DEL SERVIDOR que lo muestra: agregar claude a services del host
  systemctl restart spong-client            # + spong-web (y server) en el servidor
  ls var/database/HOST/services/ | grep claude   # si el server es local
  grep "Loaded override plugin" /var/log/spong-client.log | tail -1
```

Prueba en seco sin mandar nada al server (imprime color y summary):

```bash
cd /usr/local/spong && python3 - <<'PY'
from spong import config; config.load_all()
from spong.plugin_loader import load_plugin
m = load_plugin("client", "claude")
m.send_status = lambda h, s, c, summ, msg="", ttl=0: print(c, summ, "\n" + msg)
m.check_claude("prueba")
PY
```

Ojo con el override: tiene prioridad sobre el `claude.py` que traiga un
`.deb` posterior. Si el plugin cambia en un release, borrar o reemplazar
`etc/plugins/client/claude.py` en ese host.

### 8.3 Qué significa cada estado y qué hacer

Siempre en el host y como el usuario de `users` (root en s2/mmg1):

- **rojo `sin login` / `login vencido el …` / `sesión rechazada por Anthropic (401)`**:
  `claude auth login`. El 401 con token vigente aparece si se hizo logout
  desde otro equipo o se revocó la sesión.
- **amarillo `login vence en N días`**: abrir `claude` y correr `/login`
  antes de esa fecha. Es la misma fecha (`refreshTokenExpiresAt`) con la que
  la CLI avisa "Your login expires in N days".
- **rojo `suscripción past_due/canceled/…` / `pago pendiente de autorización` /
  `sin plan Pro/Max activo`**: problema de pago en claude.ai → Facturación.
  El detalle del servicio incluye la URL de la factura si Anthropic la manda.
- **rojo `límite 5h/semana alcanzado (…, resetea HH:MM)` / `… bloqueado`**:
  cuota agotada; se destraba sola a la hora indicada. Nada que tocar.
- **amarillo `sin respuesta de api.anthropic.com (…)`**: red/DNS/proxy del
  host hacia `api.anthropic.com:443` (Claude Code tampoco anda). Transitorio
  si Anthropic está caído; no se cachea, reintenta al ciclo siguiente.
- **verde `uso s/d (token de acceso vencido hace …)`**: el host no usó la CLI
  en ~8 h; el token de acceso se renueva solo al abrirla. Normal.
- **rojo `claude no instalado` / `… no arranca`**: reinstalar la CLI
  (`curl -fsSL https://claude.ai/install.sh | bash` como ese usuario) o
  fijar la ruta en `commands.claude`. Busca `~/.local/bin/claude` del
  usuario y después el PATH del daemon (que bajo systemd no incluye
  `~/.local/bin` de nadie).

### 8.4 Cómo funciona por dentro (para no romperlo)

- Lee `~/.claude/.credentials.json` (`expiresAt` = token de acceso ~8 h;
  `refreshTokenExpiresAt` = cuándo la CLI exige re-login) y consulta con ese
  token `GET api.anthropic.com/api/oauth/profile` (`subscription_status`) y
  `GET api/oauth/usage` (lo mismo que `/usage` en la CLI). Ninguno pasa por
  `/v1/messages`: 0 tokens.
- Caché de las dos respuestas en `tmp/claude_check.json` (0600, sin tokens)
  durante `interval` s; un cambio en `.credentials.json` lo invalida.
  Borrarlo fuerza la consulta en el próximo ciclo.
- **No** ejecuta `claude -p` (gasta cuota y con credenciales inválidas se
  cuelga >150 s), **no** corre `claude auth status` (escribe `.claude.json`
  + backups en el dir de config: como root en el home de otro usuario deja
  archivos de root) y **no** refresca tokens (podría invalidar la sesión de
  la CLI). Si alguna vez hace falta un chequeo activo, que sea opt-in.
- Solo Linux: en macOS las credenciales van al Keychain, no a un archivo.
