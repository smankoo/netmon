#!/bin/sh

speedtest-cli --simple | sed -e "s~^~`date '+%Y-%m-%d %H:%M:%S'` | `cat /etc/timezone` | ~"
