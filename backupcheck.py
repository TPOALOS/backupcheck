#!/usr/bin/env python3
"""backupcheck - is your backup really there, fresh, plausible and readable?

Point it at a folder of backups (or one file). It checks that something exists, that the
newest backup is recent, not empty, not suspiciously smaller than the one before it, and
with --verify that the archive can actually be read to the end (tar, zip, gzip, bzip2,
xz) and matches its .sha256 file if there is one. Nothing is extracted or modified.
Standard library only. Exit code 0 = all fine, 1 = problems found, 2 = usage error.
"""
import argparse
import bz2
import fnmatch
import gzip
import hashlib
import json
import lzma
import os
import re
import sys
import tarfile
import time
import zipfile

__version__ = "1.0.0"

CHUNK = 1 << 20
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
              "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """'90m', '26h', '2d', '1w', or plain seconds -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", str(text).lower())
    if not m:
        raise ValueError("not a duration: %r (use e.g. 90m, 26h, 2d)" % (text,))
    return float(m.group(1)) * TIME_UNITS[m.group(2) or "s"]


def parse_size(text):
    """'1', '10KB', '5 MiB' -> bytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]*)\s*", str(text).lower())
    if not m or (m.group(2) and m.group(2) not in SIZE_UNITS):
        raise ValueError("not a size: %r (use e.g. 1, 10KB, 5MiB)" % (text,))
    return int(float(m.group(1)) * SIZE_UNITS.get(m.group(2) or "b"))


def parse_percent(text):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", str(text))
    if not m or float(m.group(1)) > 100:
        raise ValueError("not a percentage: %r" % (text,))
    return float(m.group(1)) / 100


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return "%d B" % n if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1000.0


def human_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % round(seconds / 60)
    if seconds < 172800:
        return "%.1fh" % (seconds / 3600)
    return "%dd %dh" % (seconds // 86400, seconds % 86400 // 3600)


def list_backups(path, pattern="*"):
    """Regular files (newest first) as dicts. A file path is treated as a single backup."""
    if os.path.isfile(path):
        files = [path]
    else:
        files = [os.path.join(path, n) for n in os.listdir(path) if fnmatch.fnmatch(n, pattern)]
    out = []
    for f in files:
        try:
            st = os.lstat(f)
        except OSError:
            continue
        if os.path.isfile(f) and not os.path.islink(f):
            out.append({"name": os.path.basename(f), "path": f, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda b: (-b["mtime"], b["name"]))
    return out


def _drain(fh):
    total = 0
    for chunk in iter(lambda: fh.read(CHUNK), b""):
        total += len(chunk)
    return total


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def read_checksum_file(path):
    """Expected hex digest from `<path>.sha256` / `.sha256sum` (first token), or None."""
    for suffix in (".sha256", ".sha256sum"):
        side = path + suffix
        if os.path.isfile(side):
            try:
                with open(side, errors="replace") as fh:
                    token = fh.read().split()[:1]
            except OSError:
                return "unreadable"
            return token[0].lower() if token and re.fullmatch(r"[0-9a-fA-F]{64}", token[0]) else "malformed"
    return None


def verify_file(path):
    """Read the whole archive without extracting it. Returns (status, detail); status is ok/fail/unknown."""
    expected = read_checksum_file(path)
    if expected in ("unreadable", "malformed"):
        return "fail", "the .sha256 file next to it is %s" % expected
    if expected is not None and sha256_of(path) != expected:
        return "fail", "sha256 does not match the .sha256 file"
    low = path.lower()
    try:
        if low.endswith(TAR_SUFFIXES):
            # tarfile stops at the end-of-archive marker and never reaches the compression trailer
            # (CRC), so read the whole compressed stream first to catch corruption there.
            for suffixes, opener in ((("tar.gz", ".tgz"), gzip.open), ((".tar.bz2", ".tbz2"), bz2.open),
                                     ((".tar.xz", ".txz"), lzma.open)):
                if low.endswith(tuple(x if x.startswith(".") else "." + x for x in suffixes)):
                    with opener(path, "rb") as raw:
                        _drain(raw)
            members = 0
            with tarfile.open(path, "r:*") as tar:
                for member in tar:
                    members += 1
                    if member.isreg():
                        fh = tar.extractfile(member)
                        if fh is not None:
                            _drain(fh)
            if members == 0:
                return "fail", "the archive contains no files"
            return "ok", "%d entries read%s" % (members, ", checksum matches" if expected else "")
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad is not None:
                    return "fail", "corrupt member %r" % bad
                n = len(z.namelist())
            if n == 0:
                return "fail", "the archive contains no files"
            return "ok", "%d entries tested%s" % (n, ", checksum matches" if expected else "")
        for suffix, opener in ((".gz", gzip.open), (".bz2", bz2.open), (".xz", lzma.open)):
            if low.endswith(suffix):
                with opener(path, "rb") as fh:
                    size = _drain(fh)
                return "ok", "decompressed %s%s" % (human_size(size), ", checksum matches" if expected else "")
    except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError, lzma.LZMAError, ValueError, zlib_error()) as exc:
        return "fail", "unreadable: %s" % (str(exc) or exc.__class__.__name__)
    if expected is not None:
        return "ok", "checksum matches (no archive format test for this file type)"
    return "unknown", "no integrity test for this file type; only size and age were checked"


def zlib_error():
    import zlib
    return zlib.error


def evaluate(path, pattern="*", max_age=26 * 3600, min_size=1, max_shrink=0.5, min_count=1, verify=False, now=None):
    now = time.time() if now is None else now
    result = {"path": path, "backups": 0, "newest": None, "problems": [], "notes": [], "verified": None}
    problem = lambda code, text: result["problems"].append({"code": code, "message": text})
    backups = list_backups(path, pattern)
    result["backups"] = len(backups)
    if not backups:
        problem("no-backups", "no backup files found%s" % (" matching %r" % pattern if pattern != "*" else ""))
        return result
    newest = backups[0]
    age = now - newest["mtime"]
    result["newest"] = {"name": newest["name"], "size": newest["size"], "age_seconds": int(age)}
    if age > max_age:
        problem("stale", "newest backup %s is %s old (limit %s)" % (newest["name"], human_age(age), human_age(max_age)))
    if age < -300:
        result["notes"].append("newest backup is dated in the future (clock problem?)")
    if newest["size"] == 0:
        problem("empty", "newest backup %s is empty" % newest["name"])
    elif newest["size"] < min_size:
        problem("too-small", "newest backup %s is only %s (minimum %s)" % (
            newest["name"], human_size(newest["size"]), human_size(min_size)))
    if len(backups) >= 2 and backups[1]["size"] > 0 and newest["size"] > 0:
        prev = backups[1]
        if newest["size"] < prev["size"] * (1 - max_shrink):
            problem("shrunk", "newest backup is %s but the one before (%s) was %s: it shrank by %.0f%%" % (
                human_size(newest["size"]), prev["name"], human_size(prev["size"]),
                100 * (1 - newest["size"] / prev["size"])))
    if len(backups) < min_count:
        problem("too-few", "only %d backup(s) kept, expected at least %d" % (len(backups), min_count))
    if verify and newest["size"] > 0:
        status, detail = verify_file(newest["path"])
        result["verified"] = status
        if status == "fail":
            problem("corrupt", "%s failed verification: %s" % (newest["name"], detail))
        else:
            result["notes"].append("%s: %s" % (newest["name"], detail))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check that your backups exist, are fresh, plausible and readable.")
    ap.add_argument("paths", nargs="+", help="backup folder(s) or file(s)")
    ap.add_argument("--pattern", default="*", help="only consider files matching this glob (default *)")
    ap.add_argument("--max-age", default="26h", help="newest backup must be younger than this (default 26h)")
    ap.add_argument("--min-size", default="1", help="newest backup must be at least this big (default 1 byte)")
    ap.add_argument("--max-shrink", default="50%", help="fail if the newest is this much smaller than the previous (default 50%%)")
    ap.add_argument("--min-count", type=int, default=1, help="expect at least this many backups to be kept")
    ap.add_argument("--verify", action="store_true", help="read the newest archive to the end and check its .sha256")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    try:
        max_age, min_size, max_shrink = parse_duration(args.max_age), parse_size(args.min_size), parse_percent(args.max_shrink)
    except ValueError as exc:
        print("backupcheck: %s" % exc, file=sys.stderr)
        return 2
    results, bad = [], False
    for path in args.paths:
        if not os.path.exists(path):
            print("backupcheck: no such path: %s" % path, file=sys.stderr)
            return 2
        res = evaluate(path, args.pattern, max_age, min_size, max_shrink, args.min_count, args.verify)
        results.append(res)
        bad = bad or bool(res["problems"])
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if r["problems"]:
                print("PROBLEM %s" % r["path"])
                for p in r["problems"]:
                    print("  %-10s %s" % (p["code"], p["message"]))
            else:
                n = r["newest"]
                print("OK      %s  newest %s (%s, %s ago); %d backup(s)" % (
                    r["path"], n["name"], human_size(n["size"]), human_age(n["age_seconds"]), r["backups"]))
            for note in r["notes"]:
                print("  note: %s" % note)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
