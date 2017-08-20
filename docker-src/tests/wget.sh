#!/bin/sh

wget --output-document=/dev/null http://speedtest.wdc01.softlayer.com/downloads/test10.zip  2>&1 | grep '\([0-9.]\+ [KM]B/s\)' | sed 's/^.*(//g' |  sed 's/).*$//g' | while read pong; do echo "`date '+%Y-%m-%d %H:%M:%S'` | `cat /etc/timezone` | : $pong"; done


