SPONG — overrides de plugins de client
=======================================

Pone aquí tus plugins modificados. SPONG los carga *antes* que los plugins
empaquetados en /usr/local/spong/spong/plugins/client/, así sobreviven a
un upgrade del paquete .deb (los archivos en este directorio no son tocados
por dpkg).

Cómo personalizar un plugin
---------------------------

1. Copiar el plugin original a este directorio:

       cp /usr/local/spong/spong/plugins/client/cpu.py \
          /usr/local/spong/etc/plugins/client/

2. Editar la versión copiada:

       nano /usr/local/spong/etc/plugins/client/cpu.py

3. Reiniciar el agente:

       systemctl restart spong-client

Reglas
------

- El nombre del archivo debe ser EXACTO al del plugin original
  (ej: cpu.py, disk.py, memory.py, sensors.py).
- Tiene que exponer la misma función check_<check>(hostname).
- Si el override tiene un error de sintaxis, SPONG lo reporta en el log y
  cae al plugin empaquetado.

Para ver la lista completa de plugins disponibles:

    ls /usr/local/spong/spong/plugins/client/

Para volver al plugin original simplemente borrar el archivo de este
directorio.
