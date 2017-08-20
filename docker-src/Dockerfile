FROM ubuntu:latest
MAINTAINER sumeet.mankoo@gmail.com

# Install the required packages
RUN apt-get update && apt-get install -y cron logrotate inetutils-ping wget speedtest-cli moreutils

ADD crontab /tmp/crontab-src
ADD logrotate.conf /etc/logrotate.conf
RUN chmod 644 /etc/logrotate.conf

# Copy test scripts
ADD tests /tests
RUN chmod 755 /tests/*

# Install crontab to run logrotate
RUN crontab /tmp/crontab-src

RUN if [ ! -d /var/log/netmon-logs ]; then mkdir /var/log/netmon-logs; fi

CMD cron && bash -c '/tests/ping.sh'
