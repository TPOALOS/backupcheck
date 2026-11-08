# Changelog

## 1.0.0

First release.

- Age, empty, too-small, shrunk and too-few checks
- `--verify` reads tar, zip, gzip, bzip2 and xz archives to the end (including the compression checksum) and checks .sha256 files, without extracting
- Cron-friendly exit codes and `--json`
