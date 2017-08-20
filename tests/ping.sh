#!/bin/bash

LOGDIR=/var/log/netmon-logs
LOGFILE=ping.log

PING_TARGET=8.8.8.8

ping ${PING_TARGET} | while read pong; do echo "`date '+%Y-%m-%d %H:%M:%S'` | `cat /etc/timezone` | : $pong"; done | tee -a ${LOGDIR}/${LOGFILE}
