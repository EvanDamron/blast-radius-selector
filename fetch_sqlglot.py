"""Download sqlglot at a commit from GitHub and unpack it. usage: fetch_sqlglot.py COMMIT DEST"""
import io
import sys
import tarfile
import urllib.request

commit, dest = sys.argv[1], sys.argv[2]
url = f"https://github.com/tobymao/sqlglot/archive/{commit}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        tar.extract(m, dest, filter="data")
