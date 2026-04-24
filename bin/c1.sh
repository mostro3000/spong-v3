#/bin/bash
( /bin/echo 'root'; /bin/sleep 1; /bin/echo 'dv449'; /bin/sleep 1; /bin/echo 'getDspInfo'; /bin/sleep 1; /bin/echo 'exit' )| /bin/nc 10.78.1.3 23 >/tmp/10.78.1.3
