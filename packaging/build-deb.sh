#!/bin/bash
# build-deb.sh — Construye los paquetes .deb de SPONG v3
# Ejecutar desde /usr/local/spong/packaging/
#
# Produce:
#   spong-server_3.6.7-1_all.deb  — servidor completo (server + network + client + web)
#   spong-client_3.6.7-1_all.deb  — solo agente cliente

set -e

VERSION="3.6.7-1"
# Directorio raíz del repo: funciona tanto en /usr/local/spong como en CI (GitHub Actions)
SPONG_SRC="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="/tmp/spong-deb-build"
OUT_DIR="$SPONG_SRC/packaging/dist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "=== SPONG v3 — Build de paquetes .deb ==="
echo "Versión: $VERSION"
echo ""

mkdir -p "$OUT_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ---------------------------------------------------------------------------
# Función auxiliar: copiar árbol de archivos excluyendo __pycache__ y .pyc
# ---------------------------------------------------------------------------
copy_tree() {
    local src="$1" dst="$2"
    mkdir -p "$dst"
    rsync -a --no-owner --no-group \
        --exclude="__pycache__" --exclude="*.pyc" --exclude="*.pyo" \
        "$src/" "$dst/"
}

# ===========================================================================
# PAQUETE: spong-server
# ===========================================================================
echo "--- Construyendo spong-server ---"

PKG="$BUILD_DIR/spong-server_${VERSION}_all"
mkdir -p "$PKG"

# DEBIAN/
cp -r "$SCRIPT_DIR/spong-server/DEBIAN" "$PKG/"
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"

# Código Python principal
copy_tree "$SPONG_SRC/spong" "$PKG/usr/local/spong/spong"

# Web
copy_tree "$SPONG_SRC/web"   "$PKG/usr/local/spong/web"

# Binarios
mkdir -p "$PKG/usr/local/spong/bin"
for f in spong-server spong-network spong-client spong-web spong-ack \
          spong-cleanup spong-message spong-status spong-client spong-migrate.py; do
    [ -f "$SPONG_SRC/bin/$f" ] && cp "$SPONG_SRC/bin/$f" "$PKG/usr/local/spong/bin/"
done
chmod +x "$PKG/usr/local/spong/bin/"*

# Configuración de ejemplo (no sobreescribir si existe)
mkdir -p "$PKG/usr/local/spong/etc"
for f in spong.yaml hosts.yaml groups.yaml message.yaml; do
    [ -f "$SPONG_SRC/etc/$f" ] && \
        cp "$SPONG_SRC/etc/$f" "$PKG/usr/local/spong/etc/${f}.example"
done
# termicas.yaml.example — sin claves reales (termicas.yaml está en .gitignore)
[ -f "$SPONG_SRC/etc/termicas.yaml.example" ] && \
    cp "$SPONG_SRC/etc/termicas.yaml.example" "$PKG/usr/local/spong/etc/"

# Directorios para plugins personalizados (overrides). Sobreviven al upgrade
# porque dpkg no toca archivos ajenos al .deb. El README explica el sistema.
for cat in network client; do
    mkdir -p "$PKG/usr/local/spong/etc/plugins/$cat"
    [ -f "$SPONG_SRC/etc/plugins/$cat/README.txt" ] && \
        cp "$SPONG_SRC/etc/plugins/$cat/README.txt" "$PKG/usr/local/spong/etc/plugins/$cat/"
done

# Systemd units
mkdir -p "$PKG/etc/systemd/system"
for svc in spong-server spong-network spong-client spong-web; do
    [ -f "/etc/systemd/system/${svc}.service" ] && \
        cp "/etc/systemd/system/${svc}.service" "$PKG/etc/systemd/system/"
done

# Integración SGT: script + unit + timer (versionados en packaging/systemd/)
if [ -f "$SPONG_SRC/packaging/systemd/spong-sgt-sync" ]; then
    mkdir -p "$PKG/usr/local/sbin"
    install -m 0755 "$SPONG_SRC/packaging/systemd/spong-sgt-sync" "$PKG/usr/local/sbin/spong-sgt-sync"
    install -m 0644 "$SPONG_SRC/packaging/systemd/spong-sgt-sync.service" "$PKG/etc/systemd/system/spong-sgt-sync.service"
    install -m 0644 "$SPONG_SRC/packaging/systemd/spong-sgt-sync.timer"   "$PKG/etc/systemd/system/spong-sgt-sync.timer"
fi

# Directorios vacíos necesarios (dpkg no empaqueta dirs vacíos sin un archivo)
for d in var/database var/rrd var/archives tmp; do
    mkdir -p "$PKG/usr/local/spong/$d"
    touch "$PKG/usr/local/spong/$d/.keep"
done

# Actualizar versión en control
sed -i "s/^Version:.*/Version: $VERSION/" "$PKG/DEBIAN/control"

# Construir
dpkg-deb --root-owner-group --build "$PKG" "$OUT_DIR/"
echo "  → $OUT_DIR/spong-server_${VERSION}_all.deb"

# ===========================================================================
# PAQUETE: spong-client
# ===========================================================================
echo ""
echo "--- Construyendo spong-client ---"

PKG="$BUILD_DIR/spong-client_${VERSION}_all"
mkdir -p "$PKG"

# DEBIAN/
cp -r "$SCRIPT_DIR/spong-client/DEBIAN" "$PKG/"
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"

# Código Python — solo lo que necesita el cliente
SPONG_LIB="$PKG/usr/local/spong/spong"
mkdir -p "$SPONG_LIB/plugins/client"

# Módulos core
for f in __init__.py client_agent.py config.py database.py models.py \
          protocol.py safe_exec.py status_sender.py daemon.py messenger.py \
          plugin_loader.py; do
    [ -f "$SPONG_SRC/spong/$f" ] && cp "$SPONG_SRC/spong/$f" "$SPONG_LIB/"
done

# Plugins cliente
copy_tree "$SPONG_SRC/spong/plugins/client" "$SPONG_LIB/plugins/client"
cp "$SPONG_SRC/spong/plugins/__init__.py" "$SPONG_LIB/plugins/" 2>/dev/null || true

# Binario cliente
mkdir -p "$PKG/usr/local/spong/bin"
cp "$SPONG_SRC/bin/spong-client" "$PKG/usr/local/spong/bin/"
chmod +x "$PKG/usr/local/spong/bin/spong-client"

# Directorio etc (vacío — postinst crea la config interactivamente)
mkdir -p "$PKG/usr/local/spong/etc"

# Directorio para plugins personalizados (overrides). Solo client en este .deb.
mkdir -p "$PKG/usr/local/spong/etc/plugins/client"
[ -f "$SPONG_SRC/etc/plugins/client/README.txt" ] && \
    cp "$SPONG_SRC/etc/plugins/client/README.txt" "$PKG/usr/local/spong/etc/plugins/client/"

# Systemd unit — generada para el paquete cliente.
# NO se copia la del sistema: en s2 (que es servidor) la unit lleva
# Requires=spong-server.service, y en un host SOLO-cliente ese servicio
# no existe -> systemd rechaza el arranque. El cliente no depende del
# server local. (fix 3.6.3)
mkdir -p "$PKG/etc/systemd/system"
cat > "$PKG/etc/systemd/system/spong-client.service" << 'UNIT'
[Unit]
Description=SPONG Client Agent (checks locales: disk, cpu, memory...)
After=network.target
Wants=network.target

[Service]
Type=simple
WorkingDirectory=/usr/local/spong
ExecStart=/usr/bin/python3 /usr/local/spong/bin/spong-client --nodaemonize
Restart=on-failure
RestartSec=10
StandardOutput=append:/var/log/spong-client.log
StandardError=append:/var/log/spong-client.log

[Install]
WantedBy=multi-user.target
UNIT

# Directorios de datos
for d in var/database tmp; do
    mkdir -p "$PKG/usr/local/spong/$d"
    touch "$PKG/usr/local/spong/$d/.keep"
done

# Actualizar versión en control
sed -i "s/^Version:.*/Version: $VERSION/" "$PKG/DEBIAN/control"

# Construir
dpkg-deb --root-owner-group --build "$PKG" "$OUT_DIR/"
echo "  → $OUT_DIR/spong-client_${VERSION}_all.deb"

# ===========================================================================
echo ""
echo "=== Paquetes generados ==="
ls -lh "$OUT_DIR/"*.deb
echo ""
echo "Instalar servidor:   dpkg -i $OUT_DIR/spong-server_${VERSION}_all.deb"
echo "Instalar cliente:    dpkg -i $OUT_DIR/spong-client_${VERSION}_all.deb"
echo ""
