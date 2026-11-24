"""How a reply's tool record (Node.tool_calls_meta) is stored.

The record is a JSON list of entries: which tools the model called, with
what input and what came of it, plus the server's own notes on the reply
("_batch", "_mode", ...). Inputs and results hold text made from the
user's writing (a search query, an artifact's name, title and
description), so the record is stored like the entry's text: encrypted
with the envelope scheme of utils/encryption.

Stored form, a JSON object:

    {"v": 1,
     "shown": [{"name": ..., "status": ...}, ...],
     "exact": true,
     "sealed": "ENC:v2:..."}

- ``sealed`` is the whole record (the JSON text the code wrote), encrypted.
  Reading gives that text back unchanged.
- ``shown`` repeats, per entry, the two fields anyone who can see the
  reply gets (utils/tool_meta.SHARED_TOOL_FIELDS: name and status). SQL
  filters on entry names (``LIKE '%"_batch"%'`` and the like) keep
  matching the stored value, and a reader that needs names only
  (``shown_entries``) does not decrypt. ``exact`` says that ``shown`` is
  what tool_meta.tool_calls_meta_for gives a viewer who is not the
  reply's owner, so that answer needs no decryption either; it is false
  when an entry's name or status is not a short word.

Rows written before this hold the list itself. They are told apart by
shape, not by a prefix inside text: the record was only ever written as
a JSON list, and the stored form is a JSON object this module wrote. A
value that is not such an object reads back as it is. While encryption
is off (local development, tests) the list is stored as it is; a value
that would itself look like the stored form is wrapped
(``{"v": 1, "shown": [], "plain": ...}``), so it reads back as written.

Node.tool_calls_meta (models.py) reads and writes through here, so code
that holds a node needs nothing from this module. Code that selects the
column on its own (``db.session.query(Node.tool_calls_meta)``) gets the
stored value and passes it to ``read`` or ``shown_entries``.
"""
import json
import logging

from backend.utils import encryption
from backend.utils.encryption import (
    decrypt_content, encrypt_with_dek, is_encryption_enabled,
)

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

# Bounds for what is repeated unencrypted. Tool names and statuses are
# short words the server defines; anything longer is left to the
# encrypted part only.
SHOWN_NAME_MAX = 64
SHOWN_STATUS_MAX = 32


def _stored_object(stored):
    """The stored form as a dict, or None when *stored* is anything else
    (an earlier row's list, text that does not parse)."""
    if not isinstance(stored, str) or not stored.startswith("{"):
        return None
    try:
        obj = json.loads(stored)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != FORMAT_VERSION:
        return None
    if isinstance(obj.get("sealed"), str) or isinstance(obj.get("plain"), str):
        return obj
    return None


def is_stored_form(stored) -> bool:
    """Whether *stored* is in this module's form (as opposed to an
    earlier row's list)."""
    return _stored_object(stored) is not None


def is_encrypted_form(stored) -> bool:
    obj = _stored_object(stored)
    return obj is not None and isinstance(obj.get("sealed"), str)


def sealed_part(stored):
    """The encrypted part of a stored value, or None: what a caller hands
    to encryption.prefetch_deks with the entries' texts."""
    obj = _stored_object(stored)
    if obj is None:
        return None
    return obj.get("sealed") if isinstance(obj.get("sealed"), str) else None


def sealed_parts(nodes):
    """The encrypted parts of *nodes*' records, for
    encryption.prefetch_deks. A record stored under the key of its
    entry's text adds no call there; one under a key of its own (a public
    reply, a row converted later, a reply loaded with a bulk writer's key)
    is then fetched with the rest instead of alone."""
    parts = (sealed_part(n.tool_calls_meta_stored) for n in nodes)
    return [p for p in parts if p]


def read(stored):
    """The record as the JSON text that was written, from a stored
    value. An earlier row's value comes back as it is; None and "" too."""
    obj = _stored_object(stored)
    if obj is None:
        return stored
    if isinstance(obj.get("sealed"), str):
        return decrypt_content(obj["sealed"])
    return obj["plain"]


def _shown(text):
    """(each dict entry's name and status where they are short words, in
    order; whether that is every dict entry's name and status as they
    are)."""
    try:
        meta = json.loads(text)
    except (TypeError, ValueError):
        return [], False
    if not isinstance(meta, list):
        return [], False
    shown, exact = [], True
    for entry in meta:
        if not isinstance(entry, dict):
            continue
        out = {}
        for field, longest in (("name", SHOWN_NAME_MAX),
                               ("status", SHOWN_STATUS_MAX)):
            if field not in entry:
                continue
            value = entry[field]
            if isinstance(value, str) and len(value) <= longest:
                out[field] = value
            else:
                exact = False
        shown.append(out)
    return shown, exact


def shown_entries(stored):
    """Each entry's name and status, without decrypting: for a reader
    that only asks which entries a record has. Works on both forms and
    on the record's JSON text."""
    obj = _stored_object(stored)
    if obj is not None:
        shown = obj.get("shown")
        return shown if isinstance(shown, list) else []
    return _shown(stored)[0]


def entry_names(node):
    """The names of the entries of *node*'s record, without decrypting:
    for the checks that ask whether a reply carries a marker or a tool's
    entry, which run over whole threads where no text is read. *node*
    is a Node, or a row of a query that selected the column."""
    stored = getattr(node, "tool_calls_meta_stored", None)
    if not isinstance(stored, str):
        stored = getattr(node, "tool_calls_meta", None)
    return {e.get("name") for e in shown_entries(stored)
            if isinstance(e, dict)}


def shown_exactly(stored):
    """The list tool_meta.tool_calls_meta_for gives a viewer who is not
    the reply's owner, before the app marker is taken out, when the
    stored value holds it as it is; else None (read the record)."""
    obj = _stored_object(stored)
    if obj is None or obj.get("exact") is not True:
        return None
    shown = obj.get("shown")
    return shown if isinstance(shown, list) else None


def store(text, near=(), dek=None):
    """The value to store for the record *text* (JSON text, not empty).

    *dek*: a data key from encryption.new_dek() shared by a bulk writer,
    as Node.set_content takes it. Without one, the key of the first of
    *near* that is an envelope is used again (the entry's stored text,
    the record's earlier stored value): values of one row under one key
    are read with one key-service call. Else a new key."""
    if not is_encryption_enabled():
        if text.lstrip().startswith("{"):
            return json.dumps({"v": FORMAT_VERSION, "shown": [],
                               "plain": text})
        return text
    pair = dek
    if pair is None:
        for candidate in near:
            sealed = sealed_part(candidate) or candidate
            pair = encryption.dek_of(sealed)
            if pair is not None:
                break
    if pair is None:
        pair = encryption.new_dek()
    shown, exact = _shown(text)
    return json.dumps({
        "v": FORMAT_VERSION,
        "shown": shown,
        "exact": exact,
        "sealed": encrypt_with_dek(text, pair),
    })
