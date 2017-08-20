#!/bin/sh

speedtest-cli --simple | while read pong; do echo "`date '+%Y-%m-%d %H:%M:%S'` | `cat /etc/timezone` | : $pong"; done
