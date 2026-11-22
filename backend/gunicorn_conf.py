"""Gunicorn settings shared by every way the app server is started
(write-or-perish.service, docker-compose.prod.yml, docker-compose.staging.yml):
each passes `-c backend/gunicorn_conf.py` from the project root.

Only the access log's line format lives here; workers, bind address and log
files stay on each command line.
"""

# One line per request: address, time, method, path, protocol, status, size
# and client. The path is written without the part after the question mark
# (`%(U)s`, where gunicorn's default `%(r)s` is the whole request line), and
# the referring page's address is left out: both can hold what a person
# typed or a one-time link, and neither is needed to follow a request.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'
