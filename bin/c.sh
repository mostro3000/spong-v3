#/bin/bash
( echo 'root'; sleep 1; echo 'd626'; sleep 1; echo 'getDspInfo'; sleep 1; echo 'exit' )| /bin/nc $1 23
