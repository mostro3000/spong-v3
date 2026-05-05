SPONG — overrides de plugins de network
========================================

Pone aquí tus plugins modificados. SPONG los carga *antes* que los plugins
empaquetados en /usr/local/spong/spong/plugins/network/, así sobreviven a
un upgrade del paquete .deb (los archivos en este directorio no son tocados
por dpkg).

Cómo personalizar un plugin
---------------------------

1. Copiar el plugin original a este directorio:

       cp /usr/local/spong/spong/plugins/network/http.py \
          /usr/local/spong/etc/plugins/network/

2. Editar la versión copiada:

       nano /usr/local/spong/etc/plugins/network/http.py

3. Reiniciar el agente:

       systemctl restart spong-network

Reglas
------

- El nombre del archivo debe ser EXACTO al del plugin original
  (ej: http.py, ssh.py, dns.py, camara.py).
- Tiene que exponer la misma función check_<servicio>(hostname).
- Imports relativos como `from . import _camara` siguen funcionando y
  resuelven a los módulos hermanos del paquete instalado.
- Si el override tiene un error de sintaxis, SPONG lo reporta en el log y
  cae al plugin empaquetado.

Para ver la lista completa de plugins disponibles:

    ls /usr/local/spong/spong/plugins/network/

Para volver al plugin original simplemente borrar el archivo de este
directorio.
