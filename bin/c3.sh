#/bin/bash
( /bin/echo 'root'; /bin/sleep 1; /bin/echo 'dv449'; /bin/sleep 1; /bin/echo 'getDspInfo'; /bin/sleep 1; /bin/echo 'exit' )| /bin/nc 192.168.103.213 23 >/tmp/dvr2-r
