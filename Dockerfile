FROM ubuntu:latest
MAINTAINER sumeet.mankoo@gmail.com

# Install the required packages
RUN apt-get update && apt-get install -y cron logrotate inetutils-ping wget speedtest-cli

ADD crontab /crontab-src
ADD logrotate.conf /etc/logrotate.conf
RUN chmod 644 /etc/logrotate.conf

# Install crontab to run logrotate
RUN crontab /crontab-src

# Create an empty log so that the initital tail -f doesn't fail
RUN touch /var/log/ping.log

CMD cron && ping 8.8.8.8 >> /var/log/ping.log && tail -f /var/log/ping.log