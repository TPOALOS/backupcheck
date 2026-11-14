# backupcheck

**Is your backup really there?** Most people only find out a backup was broken on the day they need it.
backupcheck looks at a backup folder (or a single file) and tells you, in one line and one exit code, whether
the newest backup exists, is recent, is not empty, did not suddenly shrink, and can actually be read to the end.

Nothing is extracted or modified. One file, standard library only.

```
$ backupcheck /backups/db --max-age 26h --verify
OK      /backups/db  newest db-2026-11-08.tar.gz (1.2 GB, 3.1h ago); 14 backup(s)
  note: db-2026-11-08.tar.gz: 8412 entries read

$ backupcheck /backups/db --verify
PROBLEM /backups/db
  stale      newest backup db-2026-11-04.tar.gz is 4d 3h old (limit 26h)
  shrunk     newest backup is 31.0 MB but the one before (db-2026-11-03.tar.gz) was 1.2 GB: it shrank by 97%
  corrupt    db-2026-11-04.tar.gz failed verification: unreadable: Compressed file ended before the end-of-stream marker was reached
```

## Install and use

```bash
pip install git+https://github.com/TPOALOS/backupcheck       # or just copy backupcheck.py
backupcheck /backups/db                                       # default: newest must be under 26 hours old
backupcheck /backups/db /backups/photos --max-age 8d --pattern '*.tar.zst' --min-count 4
backupcheck /backups/db --verify --json
```

Cron example (mail yourself only when something is wrong):

```
30 7 * * *  backupcheck /backups/db --verify || echo "backup problem on $(hostname)" | mail -s backups me@example.com
```

## What it checks

| Problem | Meaning |
|---|---|
| `no-backups` | nothing in the folder (or nothing matching `--pattern`) |
| `stale` | the newest backup is older than `--max-age` (default 26h; accepts `90m`, `26h`, `2d`, `1w`) |
| `empty`, `too-small` | the newest file is empty, or smaller than `--min-size` (`1`, `10KB`, `5MiB`) |
| `shrunk` | the newest is more than `--max-shrink` (default 50%) smaller than the one before: the typical sign of a silently broken dump |
| `too-few` | fewer than `--min-count` backups are kept (rotation went wrong) |
| `corrupt` | with `--verify`: the archive cannot be read to the end, or does not match its `.sha256` / `.sha256sum` file |

`--verify` reads the newest backup completely **without extracting it**: `.tar`, `.tar.gz/.tgz`, `.tar.bz2`, `.tar.xz`
(including the compression checksum at the end of the stream, which `tar` itself never reaches), `.zip`, `.gz`, `.bz2`, `.xz`.
Other file types are checked for size and age only and reported as such. Archive member names are never used as paths.

Exit codes: `0` all fine, `1` problems, `2` usage error. `--json` prints machine-readable results.

## Limits

It proves a backup is **readable**, not that it contains the right data. A restore test of your own is still the real proof;
this tool makes the common failures (nothing ran, empty dump, truncated upload, rotation bug) impossible to miss.

## Tests

```bash
python3 -m unittest -v
```

## License

MIT (c) TPOALOS

---

**More tools by [TPOALOS](https://github.com/TPOALOS):** [jobwatch](https://github.com/TPOALOS/jobwatch) · [diskwatch](https://github.com/TPOALOS/diskwatch) · [expirywatch](https://github.com/TPOALOS/expirywatch) · [all 51 tools](https://tpoalos.github.io)
