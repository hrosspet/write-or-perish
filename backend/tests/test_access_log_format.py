"""The access logs hold each request's path without the part after the
question mark, and no referring page.

- The app server: write-or-perish.service and the two compose files give
  gunicorn its line format on the command line (--access-logformat), so
  no file in the working tree is needed to start it. The tests read each
  command as systemd or compose would hand it on, build gunicorn's own
  access logger from its arguments, log one request the way a worker
  does, and read the line back.
- nginx: both config files declare a log format of their own and every
  server block writes its access log with it.

No server is started and no network is used."""
import datetime
import logging
import re
import shlex
from pathlib import Path
from urllib.parse import quote

import pytest
from gunicorn.config import Config
from gunicorn.glogging import Logger

ROOT = Path(__file__).resolve().parents[2]
SERVICE = "write-or-perish.service"
COMPOSE = ("docker-compose.prod.yml", "docker-compose.staging.yml")


def _service_command():
    """The shell command of the unit's ExecStart as bash receives it:
    the text between systemd's single quotes with each %% read as %."""
    unit = (ROOT / SERVICE).read_text()
    line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    inner = re.fullmatch(r"ExecStart=/bin/bash -c '([^']*)'", line).group(1)
    # Nothing systemd would rewrite besides %%: no backslash (an escape),
    # no dollar sign (a variable), no single % (a specifier).
    assert "\\" not in inner and "$" not in inner
    assert "%" not in inner.replace("%%", "")
    return inner.replace("%%", "%").split("&&")[-1]


def _compose_command(name):
    """The command of a compose file's backend service as compose splits
    it. Compose rewrites only dollar signs; the line holds none."""
    lines = [ln for ln in (ROOT / name).read_text().splitlines()
             if ln.strip().startswith("command: gunicorn ")]
    assert len(lines) == 1, name
    assert "$" not in lines[0] and "\\" not in lines[0]
    return lines[0].split("command: ", 1)[1]


def _commands():
    return [(SERVICE, _service_command())] + [
        (name, _compose_command(name)) for name in COMPOSE]


def _gunicorn_args(command):
    words = shlex.split(command)
    assert words[0] == "gunicorn" and words[-1] == "backend.app:app"
    return words[1:-1]


class _Resp:
    status = "200 OK"
    status_code = 200
    sent = 512
    headers = []


class _Req:
    headers = []


def _logged_line(tmp_path, command, path, query, referer):
    args = _gunicorn_args(command)
    access_file = tmp_path / "gunicorn-access.log"
    for flag, target in (("--access-logfile", access_file),
                         ("--error-logfile", tmp_path / "gunicorn-error.log")):
        args[args.index(flag) + 1] = str(target)
    if "--daemon" in args:
        args.remove("--daemon")
    if "--pid" in args:
        i = args.index("--pid")
        del args[i:i + 2]

    cfg = Config()
    parsed = cfg.parser().parse_args(args)
    # No settings file: the command line is all gunicorn needs.
    assert parsed.config is None
    for key, value in vars(parsed).items():
        if value is not None and key != "args":
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


@pytest.mark.parametrize("name,command", _commands())
def test_app_server_logs_a_search_without_its_words(tmp_path, name, command):
    words = "a walk by the river"
    written = _logged_line(
        tmp_path, command, "/api/search/semantic",
        "q=" + quote(words) + "&per_page=20",
        "https://loore.org/search?q=" + quote(words))
    assert written.startswith("127.0.0.1 ")
    assert written.endswith(
        "] GET /api/search/semantic HTTP/1.0 200 512 test-client\n")
    assert "river" not in written
    assert "?" not in written


@pytest.mark.parametrize("name,command", _commands())
def test_app_server_logs_a_sign_in_link_without_its_token(
        tmp_path, name, command):
    written = _logged_line(tmp_path, command, "/auth/magic-link/verify",
                           "token=one-time-value", "")
    assert " GET /auth/magic-link/verify HTTP/1.0 200 " in written
    assert "one-time-value" not in written


def test_every_gunicorn_command_gives_the_same_format_as_one_word():
    formats = set()
    for name, command in _commands():
        args = _gunicorn_args(command)
        assert args.count("--access-logformat") == 1, name
        fmt = args[args.index("--access-logformat") + 1]
        # The whole request line, the query and the referring page are
        # not among the fields.
        fields = set(re.findall(r"%\((\w+)\)s", fmt))
        assert fields == {"h", "l", "u", "t", "m", "U", "H", "s", "b", "a"}
        # Nothing a shell, systemd or compose reads as its own.
        assert not set(fmt) & set("\"'\\$`")
        assert "-c" not in args and "--config" not in args, name
        formats.add(fmt)
    assert len(formats) == 1


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
