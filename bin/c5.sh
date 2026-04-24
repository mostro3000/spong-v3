#/bin/bash
( /bin/sleep 2; /bin/echo 'root'; /bin/sleep 2; /bin/echo 'd626'; /bin/sleep 1; /bin/echo 'cd /proc/umap'; /bin/sleep 1; /bin/echo 'cat md'; /bin/sleep 1; /bin/echo 'exit' )| /bin/nc 10.230.10.58 23 >/tmp/dvr4-imasl-1piso-r
