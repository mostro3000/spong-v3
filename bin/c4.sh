#/bin/bash
( /bin/echo 'root'; /bin/sleep 1; /bin/echo 'd626'; /bin/sleep 1; /bin/echo 'cd /proc/umap'; /bin/sleep 1; /bin/echo 'cat md'; /bin/sleep 1; /bin/echo 'exit' )| /bin/nc 10.230.32.2 23 >/tmp/dvr3-sur-pb-bl2
