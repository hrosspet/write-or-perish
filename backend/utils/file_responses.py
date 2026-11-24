"""Answering a request with a temporary file (a decrypted copy, a merged
download) so that no copy of it stays on disk."""
import os

from flask import send_file


def send_temporary_file(path, on_close=None, **send_file_args):
    """``send_file(path, **send_file_args)`` for a temporary file only this
    request uses. send_file opens the file; its name is removed right
    after, so its bytes stay readable through the open file and are freed
    when the response is closed or the process ends. The name is removed
    also when send_file raises (416 for a range outside the file).

    *on_close* runs once the server is done with the response: sent in
    full, cut off, or never started. send_file marks its response
    ``direct_passthrough``, and a WSGI server then closes only the file,
    never the response, so callbacks given to ``call_on_close`` would not
    run; without the mark the server reads and closes the response, which
    closes the file and runs them. Range requests
    (``conditional=True``) are answered as send_file answers them."""
    try:
        resp = send_file(path, **send_file_args)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    resp.direct_passthrough = False
    if on_close is not None:
        resp.call_on_close(on_close)
    return resp
