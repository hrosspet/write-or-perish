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
"/". Large objects move between storage and a local file in chunks
(``download``, ``upload``), never as one bytes object; ``read_bytes`` is
for the small ones (a manifest, a marker).
"""
import os
import shutil
import tempfile

# HEURISTIC: seconds one storage call may take before it fails. A
# manifest read happens inside a request (the Yes on "Start from your
# tweets"); a bundle download runs in a background task and may take
# longer (DOWNLOAD_TIMEOUT_SECONDS).
CALL_TIMEOUT_SECONDS = 10
DOWNLOAD_TIMEOUT_SECONDS = 600


class StorageError(Exception):
    """A storage call failed (network, permissions, a missing bucket)."""


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

    def write_bytes(self, name, data, content_type="application/json"):
        raise NotImplementedError

    def download(self, name, dest_path):
        """Copy the object to ``dest_path`` in chunks. False when it does
        not exist."""
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

    def download(self, name, dest_path):
        path = self._path(name)
        if not os.path.isfile(path):
            return False
        shutil.copyfile(path, dest_path)
        return True

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


class GCSStorage(BundleStorage):
    """A bucket, optionally under a prefix. The client comes from the
    environment's default credentials (the VM's service account), made on
    first use, so importing this module needs no network and no
    google-cloud-storage package."""

    def __init__(self, bucket, prefix="", client=None):
        self.bucket_name = bucket
        self.prefix = (prefix or "").strip("/")
        self._client = client

    def _bucket(self):
        if self._client is None:
            from google.cloud import storage
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def _key(self, name):
        name = _clean(name)
        return f"{self.prefix}/{name}" if self.prefix else name

    @staticmethod
    def _not_found(e):
        return type(e).__name__ == "NotFound" or getattr(e, "code", None) == 404

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - one error type for callers
            if self._not_found(e):
                raise
            raise StorageError(f"storage call failed ({type(e).__name__})") from e

    def exists(self, name):
        blob = self._bucket().blob(self._key(name))
        return bool(self._call(blob.exists, timeout=CALL_TIMEOUT_SECONDS))

    def read_bytes(self, name):
        blob = self._bucket().blob(self._key(name))
        try:
            return self._call(blob.download_as_bytes,
                              timeout=CALL_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            if self._not_found(e):
                return None
            raise

    def write_bytes(self, name, data, content_type="application/json"):
        blob = self._bucket().blob(self._key(name))
        self._call(blob.upload_from_string, data, content_type=content_type,
                   timeout=CALL_TIMEOUT_SECONDS)

    def download(self, name, dest_path):
        blob = self._bucket().blob(self._key(name))
        try:
            # Chunked download to the file; the library checks its own
            # checksum on the way, the caller its sha256 after.
            self._call(blob.download_to_filename, dest_path,
                       timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            if self._not_found(e):
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                return False
            raise
        return True

    def upload(self, name, src_path, content_type="application/gzip"):
        blob = self._bucket().blob(self._key(name))
        self._call(blob.upload_from_filename, src_path,
                   content_type=content_type, timeout=DOWNLOAD_TIMEOUT_SECONDS)

    def delete(self, name):
        blob = self._bucket().blob(self._key(name))
        try:
            self._call(blob.delete, timeout=CALL_TIMEOUT_SECONDS)
        except Exception as e:  # noqa: BLE001
            if self._not_found(e):
                return False
            raise
        return True

    def list(self, prefix):
        key = self._key(prefix) if (prefix or "").strip("/") else self.prefix
        cut = len(self.prefix) + 1 if self.prefix else 0
        if self._client is None:
            self._bucket()
        blobs = self._call(self._client.list_blobs, self.bucket_name,
                           prefix=(key + "/") if key else None,
                           timeout=CALL_TIMEOUT_SECONDS)
        for blob in blobs:
            yield blob.name[cut:]


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
