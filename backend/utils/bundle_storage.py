"""Where pre-fill bundles are kept (#698): a small storage interface with
two implementations.

- ``GCSStorage``: a Google Cloud Storage bucket, optionally under a
  prefix. The lab writes to it and production reads from it, each with
  its own credentials (docs/PREFILL-BUNDLES.md).
- ``LocalDirStorage``: a directory, for tests and local development.

``storage_for(location)`` picks one from a location string:
``gs://<bucket>[/<prefix>]`` or a directory path (``file://`` optional).
An empty location gives None: nothing is read or written.

Object names are relative ("bundles/<x_id>/manifest.json") and always use
"/". A bundle is uploaded from a local file in chunks (``upload``) and
read back one part at a time by byte range (``read_range``); nothing
reads a whole bundle into memory. ``read_bytes`` is for the small
objects (a manifest, a marker).

Every failure of the storage service, its credentials included, is a
StorageError, raised within seconds: each call has a short timeout and no
retries of the library's own (its default retry waits up to two
minutes). The callers retry where it makes sense: a load's next run.
"""
import os
import shutil
import tempfile

# HEURISTIC: seconds one storage call may take before it fails (small
# reads and writes, one bundle part), and an upload of a whole bundle
# from the lab.
CALL_TIMEOUT_SECONDS = 10
UPLOAD_TIMEOUT_SECONDS = 600


class StorageError(Exception):
    """A storage call failed (network, credentials, permissions, a
    missing bucket)."""


def _clean(name):
    name = (name or "").strip("/")
    if not name or ".." in name.split("/"):
        raise ValueError(f"invalid object name: {name!r}")
    return name


class BundleStorage:
    """The calls the bundle code makes. Names are relative to the
    location; a missing object reads as None / False, never an error."""

    def exists(self, name):
        raise NotImplementedError

    def read_bytes(self, name):
        """The object's bytes, or None. Small objects only."""
        raise NotImplementedError

    def read_range(self, name, start, length):
        """``length`` bytes of the object from ``start``, or None when it
        does not exist."""
        raise NotImplementedError

    def write_bytes(self, name, data, content_type="application/json"):
        raise NotImplementedError

    def upload(self, name, src_path, content_type="application/gzip"):
        """Copy a local file to the object in chunks."""
        raise NotImplementedError

    def delete(self, name):
        """True when an object was deleted."""
        raise NotImplementedError

    def list(self, prefix):
        """Names (relative) of the objects under ``prefix``."""
        raise NotImplementedError


class LocalDirStorage(BundleStorage):
    """Objects are files under ``root``. Writes go to a temporary file in
    the same directory and are renamed into place, so a reader never sees
    half an object."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def _path(self, name):
        return os.path.join(self.root, *_clean(name).split("/"))

    def exists(self, name):
        return os.path.isfile(self._path(name))

    def read_bytes(self, name):
        path = self._path(name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()

    def read_range(self, name, start, length):
        path = self._path(name)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(length)

    def _atomic(self, name):
        path = self._path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".part-")
        return path, fd, tmp

    def write_bytes(self, name, data, content_type="application/json"):
        path, fd, tmp = self._atomic(name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def upload(self, name, src_path, content_type="application/gzip"):
        path, fd, tmp = self._atomic(name)
        os.close(fd)
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, path)

    def delete(self, name):
        path = self._path(name)
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True

    def list(self, prefix):
        base = self._path(prefix) if (prefix or "").strip("/") else self.root
        if not os.path.isdir(base):
            return
        for dirpath, _dirs, files in os.walk(base):
            for fname in sorted(files):
                if fname.startswith(".part-"):
                    continue
                full = os.path.join(dirpath, fname)
                yield os.path.relpath(full, self.root).replace(os.sep, "/")


class _Missing(Exception):
    """An object that does not exist (inside GCSStorage only)."""


class GCSStorage(BundleStorage):
    """A bucket, optionally under a prefix. The client comes from the
    environment's default credentials (the VM's service account), made on
    first use inside the error handling, so importing this module needs no
    network and no google-cloud-storage package, and missing credentials
    are a StorageError like any other failure."""

    def __init__(self, bucket, prefix="", client=None):
        self.bucket_name = bucket
        self.prefix = (prefix or "").strip("/")
        self._client = client

    def _key(self, name):
        name = _clean(name)
        return f"{self.prefix}/{name}" if self.prefix else name

    @staticmethod
    def _not_found(e):
        return type(e).__name__ == "NotFound" or getattr(e, "code", None) == 404

    def _call(self, fn):
        """Run ``fn(client)``: a missing object is _Missing, every other
        failure (credentials, network, timeouts, permissions) a
        StorageError."""
        try:
            if self._client is None:
                from google.cloud import storage
                self._client = storage.Client()
            return fn(self._client)
        except Exception as e:  # noqa: BLE001 - one error type for callers
            if self._not_found(e):
                raise _Missing() from e
            raise StorageError(
                f"storage call failed ({type(e).__name__})") from e

    def _blob(self, client, name):
        return client.bucket(self.bucket_name).blob(self._key(name))

    def exists(self, name):
        try:
            return bool(self._call(lambda c: self._blob(c, name).exists(
                timeout=CALL_TIMEOUT_SECONDS, retry=None)))
        except _Missing:
            return False

    def read_bytes(self, name):
        try:
            return self._call(lambda c: self._blob(c, name).download_as_bytes(
                timeout=CALL_TIMEOUT_SECONDS, retry=None))
        except _Missing:
            return None

    def read_range(self, name, start, length):
        # end is inclusive; a partial download has no whole-object
        # checksum (the caller checks the part's own sha256).
        try:
            return self._call(lambda c: self._blob(c, name).download_as_bytes(
                start=start, end=start + length - 1, checksum=None,
                timeout=CALL_TIMEOUT_SECONDS, retry=None))
        except _Missing:
            return None

    def write_bytes(self, name, data, content_type="application/json"):
        self._call(lambda c: self._blob(c, name).upload_from_string(
            data, content_type=content_type, timeout=CALL_TIMEOUT_SECONDS,
            retry=None))

    def upload(self, name, src_path, content_type="application/gzip"):
        self._call(lambda c: self._blob(c, name).upload_from_filename(
            src_path, content_type=content_type,
            timeout=UPLOAD_TIMEOUT_SECONDS))

    def delete(self, name):
        try:
            self._call(lambda c: self._blob(c, name).delete(
                timeout=CALL_TIMEOUT_SECONDS, retry=None))
        except _Missing:
            return False
        return True

    def list(self, prefix):
        key = self._key(prefix) if (prefix or "").strip("/") else self.prefix
        cut = len(self.prefix) + 1 if self.prefix else 0
        try:
            names = self._call(lambda c: [b.name for b in c.list_blobs(
                self.bucket_name, prefix=(key + "/") if key else None,
                timeout=CALL_TIMEOUT_SECONDS, retry=None)])
        except _Missing:
            return
        for name in names:
            yield name[cut:]


def storage_for(location):
    """The storage a location names, or None when it is empty."""
    location = (location or "").strip()
    if not location:
        return None
    if location.startswith("gs://"):
        rest = location[len("gs://"):].strip("/")
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError("a gs:// location needs a bucket name")
        return GCSStorage(bucket, prefix)
    if location.startswith("file://"):
        location = location[len("file://"):]
    return LocalDirStorage(location)
