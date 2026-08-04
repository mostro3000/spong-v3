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
