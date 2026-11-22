"""The access logs hold each request's path without the part after the
question mark, and no referring page.

- The app server: write-or-perish.service and the two compose files start
  gunicorn with `-c backend/gunicorn_conf.py`. The test builds gunicorn's own
  access logger from the service file's arguments and that file, logs one
  request the way a worker does, and reads the line back.
- nginx: both config files declare a log format of their own and every
  server block writes its access log with it.

No server is started and no network is used."""
import datetime
import logging
import re
import runpy
import shlex
from pathlib import Path
from urllib.parse import quote

from gunicorn.config import Config
from gunicorn.glogging import Logger

ROOT = Path(__file__).resolve().parents[2]
CONF = "backend/gunicorn_conf.py"


def _service_gunicorn_args():
    unit = (ROOT / "write-or-perish.service").read_text()
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    inner = re.search(r"-c '(.*)'\s*$", line).group(1)
    words = shlex.split(inner.split("&&")[-1])
    assert words[0] == "gunicorn"
    return words[1:-1]  # without "gunicorn" and the app module


class _Resp:
    status = "200 OK"
    status_code = 200
    sent = 512
    headers = []


class _Req:
    headers = []


def _logged_line(tmp_path, path, query, referer):
    args = _service_gunicorn_args()
    access_file = tmp_path / "gunicorn-access.log"
    for flag, target in (("--access-logfile", access_file),
                         ("--error-logfile", tmp_path / "gunicorn-error.log")):
        args[args.index(flag) + 1] = str(target)
    args.remove("--daemon")
    i = args.index("--pid")
    del args[i:i + 2]

    cfg = Config()
    parsed = cfg.parser().parse_args(args)
    assert parsed.config == CONF
    for key, value in runpy.run_path(str(ROOT / parsed.config)).items():
        if key in cfg.settings:
            cfg.set(key, value)
    for key, value in vars(parsed).items():
        if value is not None and key not in ("args", "config"):
            cfg.set(key.lower(), value)

    environ = {
        "REQUEST_METHOD": "GET",
        "RAW_URI": path + "?" + query,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_PROTOCOL": "HTTP/1.0",
        "REMOTE_ADDR": "127.0.0.1",
        "HTTP_REFERER": referer,
        "HTTP_USER_AGENT": "test-client",
    }
    log = Logger(cfg)
    try:
        log.access(_Resp(), _Req(), environ, datetime.timedelta(seconds=1))
    finally:
        for name in ("gunicorn.access", "gunicorn.error"):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)
    return access_file.read_text()


def test_app_server_logs_a_search_without_its_words(tmp_path):
    words = "a walk by the river"
    written = _logged_line(
        tmp_path, "/api/search/semantic", "q=" + quote(words) + "&per_page=20",
        "https://loore.org/search?q=" + quote(words))
    assert '"GET /api/search/semantic HTTP/1.0" 200 512 "test-client"' in written
    assert "127.0.0.1" in written
    assert "river" not in written
    assert "?" not in written


def test_app_server_logs_a_sign_in_link_without_its_token(tmp_path):
    written = _logged_line(tmp_path, "/auth/magic-link/verify",
                           "token=one-time-value", "")
    assert '"GET /auth/magic-link/verify HTTP/1.0" 200' in written
    assert "one-time-value" not in written


def test_every_gunicorn_command_uses_the_shared_settings_file():
    for name in ("write-or-perish.service", "docker-compose.prod.yml",
                 "docker-compose.staging.yml"):
        commands = [ln for ln in (ROOT / name).read_text().splitlines()
                    if "--access-logfile" in ln]
        assert commands, name
        for line in commands:
            assert "gunicorn -c " + CONF + " " in line, name


def test_nginx_writes_its_access_log_in_a_format_without_the_query():
    names = set()
    for name, prefix in (("configs/nginx.txt", "loore"),
                         ("configs/nginx-staging.txt", "loore_staging")):
        text = (ROOT / name).read_text()
        fmt = prefix + "_no_query"
        declared = re.search(r"^log_format %s ([^;]+);" % fmt, text, re.M)
        assert declared, name
        variables = set(re.findall(r"\$(\w+)", declared.group(1)))
        assert prefix + "_request_path" in variables
        # The whole request line, the query and the referring page are
        # not among the fields.
        assert not variables & {"request", "request_uri", "args",
                                "query_string", "http_referer"}
        assert re.search(
            r'^map \$request_uri \$%s_request_path \{\s*"~\^\(\?<\w+>\[\^\?\]\*\)"'
            % prefix, text, re.M), name
        servers = text.count("\nserver {")
        assert servers == 2
        assert len(re.findall(
            r"^\s*access_log /var/log/nginx/access\.log %s;" % fmt,
            text, re.M)) == servers, name
        names.add(fmt)
    # Both files are loaded into one nginx: the names must differ.
    assert len(names) == 2
