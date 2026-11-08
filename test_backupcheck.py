import bz2
import contextlib
import gzip
import hashlib
import io
import json
import lzma
import os
import tarfile
import tempfile
import time
import unittest
import zipfile

import backupcheck as bc

NOW = 1_900_000_000.0
HOUR = 3600


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def put(self, name, data=b"x" * 100, age_hours=1.0):
        p = os.path.join(self.d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        t = NOW - age_hours * HOUR
        os.utime(p, (t, t))
        return p

    def tar(self, name, files=None, mode="w:gz", age_hours=1.0):
        p = os.path.join(self.d, name)
        with tarfile.open(p, mode) as tar:
            for fname, data in (files or {"a.txt": b"hello" * 100}).items():
                info = tarfile.TarInfo(fname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        t = NOW - age_hours * HOUR
        os.utime(p, (t, t))
        return p

    def codes(self, **kw):
        kw.setdefault("now", NOW)
        return [p["code"] for p in bc.evaluate(self.d, **kw)["problems"]]


class ParseTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(bc.parse_duration("90m"), 5400)
        self.assertEqual(bc.parse_duration("26h"), 93600)
        self.assertEqual(bc.parse_duration("2d"), 172800)
        self.assertEqual(bc.parse_duration("1w"), 604800)
        self.assertEqual(bc.parse_duration("45"), 45)
        self.assertEqual(bc.parse_duration("1.5h"), 5400)

    def test_bad_durations(self):
        for bad in ("", "h", "5x", "-3h", "abc"):
            with self.assertRaises(ValueError):
                bc.parse_duration(bad)

    def test_sizes(self):
        self.assertEqual(bc.parse_size("1"), 1)
        self.assertEqual(bc.parse_size("10KB"), 10000)
        self.assertEqual(bc.parse_size("5 MiB"), 5 * 1024 ** 2)
        self.assertEqual(bc.parse_size("2g"), 2 * 1024 ** 3)

    def test_bad_sizes(self):
        for bad in ("", "KB", "5 parsecs", "-1"):
            with self.assertRaises(ValueError):
                bc.parse_size(bad)

    def test_percent(self):
        self.assertEqual(bc.parse_percent("50%"), 0.5)
        self.assertEqual(bc.parse_percent("25"), 0.25)
        with self.assertRaises(ValueError):
            bc.parse_percent("150%")

    def test_human_formats(self):
        self.assertEqual(bc.human_size(512), "512 B")
        self.assertEqual(bc.human_size(1_500_000), "1.5 MB")
        self.assertEqual(bc.human_age(30), "30s")
        self.assertEqual(bc.human_age(3 * HOUR), "3.0h")
        self.assertEqual(bc.human_age(3 * 86400 + 2 * HOUR), "3d 2h")


class CheckTests(Base):
    def test_healthy(self):
        self.put("db-1.sql", age_hours=1)
        self.put("db-0.sql", age_hours=25)
        self.assertEqual(self.codes(), [])

    def test_missing_folder_content(self):
        self.assertEqual(self.codes(), ["no-backups"])

    def test_pattern_with_no_match(self):
        self.put("notes.txt")
        self.assertEqual(self.codes(pattern="*.sql"), ["no-backups"])
        self.assertIn("matching", bc.evaluate(self.d, "*.sql", now=NOW)["problems"][0]["message"])

    def test_stale(self):
        self.put("db.sql", age_hours=60)
        self.assertEqual(self.codes(), ["stale"])
        self.assertEqual(self.codes(max_age=3 * 86400), [])

    def test_only_newest_counts_for_age(self):
        self.put("old.sql", age_hours=500)
        self.put("new.sql", age_hours=2)
        self.assertEqual(self.codes(), [])

    def test_empty_newest(self):
        self.put("db.sql", data=b"", age_hours=1)
        self.assertEqual(self.codes(), ["empty"])

    def test_too_small(self):
        self.put("db.sql", data=b"x" * 10)
        self.assertEqual(self.codes(min_size=1000), ["too-small"])

    def test_shrunk(self):
        self.put("db-new.sql", data=b"x" * 100, age_hours=1)
        self.put("db-old.sql", data=b"x" * 1000, age_hours=25)
        self.assertEqual(self.codes(), ["shrunk"])
        self.assertEqual(self.codes(max_shrink=0.95), [])

    def test_growing_backups_are_fine(self):
        self.put("db-new.sql", data=b"x" * 1000, age_hours=1)
        self.put("db-old.sql", data=b"x" * 100, age_hours=25)
        self.assertEqual(self.codes(), [])

    def test_previous_empty_file_does_not_trigger_shrink(self):
        self.put("new.sql", data=b"x" * 10, age_hours=1)
        self.put("old.sql", data=b"", age_hours=25)
        self.assertEqual(self.codes(), [])

    def test_min_count(self):
        self.put("only.sql")
        self.assertEqual(self.codes(min_count=3), ["too-few"])

    def test_single_file_path(self):
        p = self.put("one.sql", age_hours=1)
        res = bc.evaluate(p, now=NOW)
        self.assertEqual((res["backups"], res["problems"]), (1, []))

    def test_directories_and_symlinks_are_ignored(self):
        os.makedirs(os.path.join(self.d, "subdir"))
        real = self.put("real.sql", age_hours=50)
        os.symlink(real, os.path.join(self.d, "link.sql"))
        names = [b["name"] for b in bc.list_backups(self.d)]
        self.assertEqual(names, ["real.sql"])

    def test_future_dated_backup_noted(self):
        self.put("future.sql", age_hours=-5)
        res = bc.evaluate(self.d, now=NOW)
        self.assertEqual(res["problems"], [])
        self.assertTrue(any("future" in n for n in res["notes"]))

    def test_multiple_problems_reported_together(self):
        self.put("new.sql", data=b"x" * 5, age_hours=70)
        self.put("old.sql", data=b"x" * 1000, age_hours=90)
        self.assertEqual(sorted(self.codes(min_size=10)), ["shrunk", "stale", "too-small"])


class VerifyTests(Base):
    def test_tar_gz_ok(self):
        p = self.tar("b.tar.gz")
        status, detail = bc.verify_file(p)
        self.assertEqual(status, "ok")
        self.assertIn("1 entries", detail)

    def test_other_tar_flavours(self):
        for name, mode in (("a.tar", "w"), ("b.tar.bz2", "w:bz2"), ("c.tar.xz", "w:xz"), ("d.tgz", "w:gz")):
            self.assertEqual(bc.verify_file(self.tar(name, mode=mode))[0], "ok", name)

    def test_truncated_tar_gz_detected(self):
        p = self.tar("b.tar.gz", files={"big.bin": os.urandom(200_000)})
        with open(p, "rb") as fh:
            data = fh.read()
        with open(p, "wb") as fh:
            fh.write(data[: len(data) // 2])
        status, detail = bc.verify_file(p)
        self.assertEqual(status, "fail")
        self.assertIn("unreadable", detail)

    def test_corrupted_bytes_detected(self):
        p = self.tar("b.tar.gz", files={"big.bin": os.urandom(100_000)})
        with open(p, "r+b") as fh:
            fh.seek(5000)
            fh.write(b"\x00" * 64)
        self.assertEqual(bc.verify_file(p)[0], "fail")

    def test_empty_tar_is_a_failure(self):
        p = os.path.join(self.d, "e.tar")
        with tarfile.open(p, "w"):
            pass
        status, detail = bc.verify_file(p)
        self.assertEqual((status, detail), ("fail", "the archive contains no files"))

    def test_not_really_a_tar(self):
        p = self.put("fake.tar.gz", data=b"this is not an archive at all" * 10)
        self.assertEqual(bc.verify_file(p)[0], "fail")

    def test_zip_ok_and_corrupt(self):
        p = os.path.join(self.d, "z.zip")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("a.txt", "hello" * 1000)
        self.assertEqual(bc.verify_file(p)[0], "ok")
        with open(p, "r+b") as fh:
            fh.seek(40)
            fh.write(b"\xff\xff\xff\xff")
        self.assertEqual(bc.verify_file(p)[0], "fail")

    def test_empty_zip(self):
        p = os.path.join(self.d, "z.zip")
        with zipfile.ZipFile(p, "w"):
            pass
        self.assertEqual(bc.verify_file(p)[0], "fail")

    def test_single_file_compressors(self):
        for name, opener in (("a.sql.gz", gzip.open), ("b.sql.bz2", bz2.open), ("c.sql.xz", lzma.open)):
            p = os.path.join(self.d, name)
            with opener(p, "wb") as fh:
                fh.write(b"INSERT INTO t VALUES (1);\n" * 1000)
            self.assertEqual(bc.verify_file(p)[0], "ok", name)
            with open(p, "rb") as fh:
                data = fh.read()
            with open(p, "wb") as fh:
                fh.write(data[: len(data) - 12])
            self.assertEqual(bc.verify_file(p)[0], "fail", name)

    def test_unknown_type_is_unknown_not_failure(self):
        p = self.put("dump.sql", data=b"SELECT 1;")
        status, detail = bc.verify_file(p)
        self.assertEqual(status, "unknown")
        self.assertIn("only size and age", detail)

    def test_checksum_sidecar_matches(self):
        p = self.put("dump.sql", data=b"SELECT 1;")
        with open(p + ".sha256", "w") as fh:
            fh.write("%s  dump.sql\n" % hashlib.sha256(b"SELECT 1;").hexdigest())
        status, detail = bc.verify_file(p)
        self.assertEqual(status, "ok")
        self.assertIn("checksum matches", detail)

    def test_checksum_mismatch(self):
        p = self.put("dump.sql", data=b"SELECT 1;")
        with open(p + ".sha256", "w") as fh:
            fh.write("0" * 64 + "\n")
        self.assertEqual(bc.verify_file(p), ("fail", "sha256 does not match the .sha256 file"))

    def test_malformed_checksum_file(self):
        p = self.put("dump.sql", data=b"SELECT 1;")
        with open(p + ".sha256", "w") as fh:
            fh.write("not a hash\n")
        status, detail = bc.verify_file(p)
        self.assertEqual(status, "fail")
        self.assertIn("malformed", detail)

    def test_checksum_applies_to_archives_too(self):
        p = self.tar("b.tar.gz")
        with open(p + ".sha256sum", "w") as fh:
            fh.write(bc.sha256_of(p) + "  b.tar.gz\n")
        self.assertIn("checksum matches", bc.verify_file(p)[1])

    def test_archive_members_are_never_extracted(self):
        p = os.path.join(self.d, "evil.tar")
        with tarfile.open(p, "w") as tar:
            info = tarfile.TarInfo("../../escape.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        self.assertEqual(bc.verify_file(p)[0], "ok")
        self.assertFalse(os.path.exists(os.path.join(self.d, "..", "..", "escape.txt")))

    def test_evaluate_with_verify_flags_corrupt_newest(self):
        p = self.put("b.tar.gz", data=b"definitely not gzip data" * 10, age_hours=1)
        res = bc.evaluate(self.d, verify=True, now=NOW)
        self.assertEqual([x["code"] for x in res["problems"]], ["corrupt"])
        self.assertEqual(res["verified"], "fail")

    def test_evaluate_with_verify_ok_adds_note(self):
        self.tar("b.tar.gz")
        res = bc.evaluate(self.d, verify=True, now=NOW)
        self.assertEqual(res["problems"], [])
        self.assertTrue(any("entries read" in n for n in res["notes"]))


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bc.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(Base):
    def fresh(self, name="db.sql", data=b"x" * 100):
        p = os.path.join(self.d, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_ok_exit_zero(self):
        self.fresh()
        code, out, _ = run_main(self.d)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("OK"))
        self.assertIn("db.sql", out)

    def test_problem_exit_one(self):
        p = self.fresh()
        old = time.time() - 5 * 86400
        os.utime(p, (old, old))
        code, out, _ = run_main(self.d)
        self.assertEqual(code, 1)
        self.assertIn("PROBLEM", out)
        self.assertIn("stale", out)

    def test_max_age_option(self):
        p = self.fresh()
        old = time.time() - 5 * 86400
        os.utime(p, (old, old))
        self.assertEqual(run_main(self.d, "--max-age", "7d")[0], 0)

    def test_json(self):
        self.fresh()
        code, out, _ = run_main(self.d, "--json")
        data = json.loads(out)
        self.assertEqual((code, data[0]["backups"], data[0]["problems"]), (0, 1, []))

    def test_several_paths_any_problem_fails(self):
        self.fresh()
        with tempfile.TemporaryDirectory() as empty:
            code, out, _ = run_main(self.d, empty)
        self.assertEqual(code, 1)
        self.assertIn("no-backups", out)

    def test_usage_errors(self):
        self.assertEqual(run_main("/nonexistent/backups")[0], 2)
        self.assertEqual(run_main(self.d, "--max-age", "soon")[0], 2)
        self.assertEqual(run_main(self.d, "--min-size", "lots")[0], 2)
        self.assertEqual(run_main(self.d, "--max-shrink", "200%")[0], 2)

    def test_verify_flag(self):
        self.fresh("b.tar.gz", data=b"garbage garbage garbage")
        code, out, _ = run_main(self.d, "--verify")
        self.assertEqual(code, 1)
        self.assertIn("corrupt", out)

    def test_pattern_flag(self):
        self.fresh("db.sql")
        self.fresh("notes.txt")
        code, out, _ = run_main(self.d, "--pattern", "*.sql", "--json")
        self.assertEqual(json.loads(out)[0]["backups"], 1)


if __name__ == "__main__":
    unittest.main()
