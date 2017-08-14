FROM ubuntu:latest
MAINTAINER sumeet.mankoo@gmail.com

# Install the required packages
RUN apt-get update && apt-get install -y cron logrotate inetutils-ping wget speedtest-cli moreutils

ADD crontab /tmp/crontab-src
ADD logrotate.conf /etc/logrotate.conf
RUN chmod 644 /etc/logrotate.conf

# Copy test scripts
ADD tests /tests

# Install crontab to run logrotate
RUN crontab /tmp/crontab-src

RUN if [ ! -d /var/log/testlogs ]; then mkdir /var/log/testlogs; fi

# Create an empty log so that the initital tail -f doesn't fail
RUN touch /var/log/testlogs/ping.log

CMD cron && ping 8.8.8.8 | sed -e "s~^~`date '+%Y-%m-%d %H:%M:%S'` | `cat /etc/timezone` | ~" >> /var/log/testlogs/ping.log && tail -f /var/log/ping.log
