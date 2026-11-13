"""An update is taken only when the release's checksum rows are signed by a trusted key."""

import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
from luvbox import release_signature, updater
from luvbox.release_signature import Key
from tests import release_server
from tests.release_server import TEST_KEY, ReleaseServer, make_program, sign_for_tests
from tools import release_key


ROWS = {"LuvBox.exe": "a" * 64, "LuvBox-1.9.2-source-licenses.zip": "b" * 64}


def listing(rows=ROWS):
    return "".join(f"{digest}  {name}\n" for name, digest in rows.items())


def signed(version="1.9.2", rows=ROWS, sign=sign_for_tests):
    return updater.sign_checksums(version, listing(rows), sign)


class VerificationTests(unittest.TestCase):
    def accepted(self, text, *, version="1.9.2", rows=ROWS, keys=(TEST_KEY,)):
        return release_signature.verify(version, text, rows, keys)

    def test_a_signature_by_a_trusted_key_over_these_rows_is_accepted(self):
        text = signed()
        self.assertTrue(self.accepted(text))
        # Windows line endings and other notes around it change nothing.
        self.assertTrue(self.accepted("note\r\n" + text.replace("\n", "\r\n") + "\r\nmore\r\n"))

    def test_anything_else_is_refused(self):
        text = signed()
        line = text.splitlines()[-1]
        identifier, encoded = line.split(" ")[3:]
        signature = base64.b64decode(encoded)
        changed_program = dict(ROWS, **{"LuvBox.exe": "c" * 64})
        renamed = {"LuvBox.exe": "a" * 64, "LuvBox-1.9.2-sources.zip": "b" * 64}
        cases = {
            "no signature line": (listing(), {}),
            "another program hash": (text, {"rows": changed_program}),
            "another file name": (text, {"rows": renamed}),
            "one row missing": (text, {"rows": {"LuvBox.exe": "a" * 64}}),
            "rows in another order": (text, {"rows": dict(reversed(list(ROWS.items())))}),
            "another version": (text, {"version": "1.9.3"}),
            "not a version": (text, {"version": "v1.9.2"}),
            "a key that is not trusted": (text, {"keys": release_signature.RELEASE_KEYS}),
            "no trusted keys": (text, {"keys": ()}),
            "one bit changed": (text.replace(encoded, base64.b64encode(
                signature[:-1] + bytes([signature[-1] ^ 1])).decode()), {}),
            "a shorter signature": (text.replace(encoded, base64.b64encode(signature[1:]).decode()), {}),
            "not base64": (text.replace(encoded, encoded[:-4] + "*" * 4), {}),
            "another key's name on it": (text.replace(identifier, "0" * 16), {}),
            "the line after other text": (text.replace("# luvbox-signature", "x # luvbox-signature"), {}),
            "another format number": (text.replace("luvbox-signature v1", "luvbox-signature v2"), {}),
        }
        for name, (candidate, changes) in cases.items():
            with self.subTest(name):
                self.assertFalse(self.accepted(candidate, **changes))

    def test_a_block_that_only_ends_with_the_right_hash_is_refused(self):
        # The classic mistake is to look for the hash inside the opened block. Here the whole
        # block must match, so these blocks made with the real private key still fail.
        message = release_signature.signed_message("1.9.2", ROWS)
        digest = hashlib.sha256(message).digest()
        size = TEST_KEY.size
        info = release_signature._SHA256_INFO
        blocks = {
            "no padding": digest.rjust(size, b"\x00"),
            "short padding, garbage after the hash": (b"\x00\x01" + b"\xff" * 8 + b"\x00" + info + digest).ljust(size, b"\x07"),
            "wrong block type": b"\x00\x02" + b"\xff" * (size - len(info) - len(digest) - 3) + b"\x00" + info + digest,
            "a hash without its algorithm name": b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest,
        }
        for name, block in blocks.items():
            with self.subTest(name):
                forged = pow(int.from_bytes(block, "big"), release_server._TEST_PRIVATE,
                             release_server._TEST_MODULUS).to_bytes(size, "big")
                self.assertFalse(release_signature.confirms(TEST_KEY, message, forged))
        # The properly made one passes, so the cases above fail for the reason given.
        self.assertTrue(release_signature.confirms(TEST_KEY, message, sign_for_tests(message)[1]))

    def test_weak_or_odd_keys_confirm_nothing(self):
        message = b"anything"
        signature = sign_for_tests(message)[1]
        self.assertTrue(release_signature.confirms(TEST_KEY, message, signature))
        small = Key((1 << 1023) + 1)
        for key in (small, Key(TEST_KEY.modulus, 1), Key(TEST_KEY.modulus, 65536), Key(TEST_KEY.modulus, 2)):
            with self.subTest(bits=key.modulus.bit_length(), exponent=key.exponent):
                self.assertFalse(release_signature.confirms(key, message, signature))
        # A value at or above the modulus is not a signature.
        too_large = (TEST_KEY.modulus + 5).to_bytes(TEST_KEY.size, "big")
        self.assertFalse(release_signature.confirms(TEST_KEY, message, too_large))

    def test_the_signed_text_names_the_version_and_every_row(self):
        self.assertEqual(release_signature.signed_message("1.9.2", ROWS),
                         b"LuvBox release 1.9.2\n" + listing().encode("ascii"))
        for version in ("", "1.9", "v1.9.2", "1.9.2\n", None, 192):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    release_signature.signed_message(version, ROWS)

    def test_packaging_refuses_a_signature_that_does_not_fit_its_rows(self):
        def for_other_rows(_message):
            return sign_for_tests(release_signature.signed_message("1.9.2", {"LuvBox.exe": "f" * 64}))
        with self.assertRaises(ValueError):
            updater.sign_checksums("1.9.2", listing(), for_other_rows)


class UpdateTests(unittest.TestCase):
    """The updater against the local stand-in for GitHub: no network."""

    def setUp(self):
        self.server = ReleaseServer()
        self.addCleanup(self.server.close)
        self.folder = Path(tempfile.mkdtemp(prefix="luvbox-signature-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def refused(self, kind, version="1.9.2"):
        with self.assertRaises(updater.UpdateError) as raised:
            updater.download(version, self.folder, self.server.source)
        self.assertEqual(raised.exception.kind, kind)
        # Refused before the large download: only the checksum file was asked for.
        self.assertEqual([path for path, _headers in self.server.requests],
                         [f"/download/v{version}/SHA256-{version}.txt"])
        self.assertEqual(list(self.folder.iterdir()) if self.folder.is_dir() else [], [])

    def test_a_signed_release_is_fetched(self):
        program = self.server.publish("1.9.2")
        path, digest = updater.download("1.9.2", self.folder, self.server.source, size=len(program))
        self.assertEqual(path.read_bytes(), program)
        self.assertEqual(digest, hashlib.sha256(program).hexdigest())

    def test_a_release_without_a_signature_is_not_fetched(self):
        self.server.publish("1.9.2", signed=False)
        self.refused("signature")
        self.assertIn("서명", str(updater.UpdateError("signature")))

    def test_a_release_whose_program_was_swapped_with_a_matching_checksum_is_not_fetched(self):
        # Someone who can change the release replaces the program and writes a new, matching
        # checksum row, keeping the old signature line. The rows no longer match what was signed.
        self.server.publish("1.9.2")
        name = "/download/v1.9.2/SHA256-1.9.2.txt"
        original = self.server.files[name].decode("ascii")
        other = make_program("1.9.2", b"not the published application")
        self.server.files["/download/v1.9.2/LuvBox.exe"] = other
        self.server.files[name] = original.replace(original[:64], hashlib.sha256(other).hexdigest()).encode("ascii")
        self.refused("signature")

    def test_another_releases_signed_file_does_not_pass_for_this_one(self):
        self.server.publish("1.9.1")
        earlier = self.server.files["/download/v1.9.1/SHA256-1.9.1.txt"]
        self.server.publish("1.9.2")
        self.server.files["/download/v1.9.2/SHA256-1.9.2.txt"] = earlier
        self.refused("signature")

    def test_a_signature_by_a_key_the_source_does_not_trust_is_not_fetched(self):
        self.server.publish("1.9.2")
        # The same server, read by a Source that trusts only the real release keys.
        strict = updater.Source(latest=self.server.source.latest, downloads=self.server.source.downloads,
                                secure=False)
        self.assertEqual(strict.keys, release_signature.RELEASE_KEYS)
        with self.assertRaises(updater.UpdateError) as raised:
            updater.download("1.9.2", self.folder, strict)
        self.assertEqual(raised.exception.kind, "signature")


@unittest.skipUnless(os.name == "nt", "the release key lives in the Windows key store")
class WindowsKeyTests(unittest.TestCase):
    """The signing tool with a throwaway key of its own; the release key is never touched."""

    def test_a_key_in_the_key_store_signs_what_the_application_then_confirms(self):
        name = release_key.TEST_PREFIX + uuid.uuid4().hex[:12]
        try:
            release_key._run("-Create", name=name)
        except release_key.KeyUnavailable:
            self.skipTest("PowerShell cannot create a key here")
        self.addCleanup(release_key._run, "-Delete", name=name)
        key = release_key.public_key(name)
        self.assertEqual((key.modulus.bit_length(), key.exponent), (3072, 65537))
        text = updater.sign_checksums("1.9.2", listing(), lambda message: release_key.sign(message, name))
        self.assertTrue(release_signature.verify("1.9.2", text, ROWS, (key,)))
        self.assertFalse(release_signature.verify("1.9.2", text, ROWS))     # not a key installed versions trust
        self.assertEqual(release_key.sign(b"twice", name)[1], release_key.sign(b"twice", name)[1])

    def test_a_key_that_is_missing_or_not_the_trusted_one_stops_the_release(self):
        missing = release_key.TEST_PREFIX + uuid.uuid4().hex[:12]
        with self.assertRaises(release_key.KeyUnavailable):
            release_key.public_key(missing)
        with patch.object(release_key, "public_key", return_value=TEST_KEY):
            with self.assertRaises(release_key.KeyUnavailable) as raised:
                release_key.require_release_key()
        self.assertIn("공개 키", str(raised.exception))

    BACKUP = Path(release_key.SCRIPT).with_name("release_key_backup.ps1")

    def backup(self, *arguments, passphrase="throwaway passphrase for a test key"):
        environment = dict(os.environ, LUVBOX_TEST_KEY_PASSPHRASE=passphrase)
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(self.BACKUP), *arguments], env=environment, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=180,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_a_backup_restores_the_same_key_and_is_refused_where_it_should_be(self):
        first = release_key.TEST_PREFIX + uuid.uuid4().hex[:12]
        second = release_key.TEST_PREFIX + uuid.uuid4().hex[:12]
        try:
            release_key._run("-Create", name=first)
        except release_key.KeyUnavailable:
            self.skipTest("PowerShell cannot create a key here")
        for name in (first, second):
            self.addCleanup(release_key._run, "-Delete", name=name)
        folder = Path(tempfile.mkdtemp(prefix="luvbox-key-backup-"))
        self.addCleanup(shutil.rmtree, folder, True)
        file = folder / "backup.p8"
        original = release_key.public_key(first)

        made = self.backup("-Export", str(file), "-Name", first)
        self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
        self.assertIn("read back", made.stdout)
        self.assertIn(original.identifier, made.stdout)
        data = file.read_bytes()
        self.assertEqual(data[0], 0x30)
        # Encrypted as a whole: not even the public number is readable in the file.
        self.assertNotIn(original.modulus.to_bytes(original.size, "big")[:32], data)

        checked = self.backup("-Verify", str(file), "-Name", first)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("same key", checked.stdout)
        self.assertNotEqual(self.backup("-Verify", str(file), "-Name", first, passphrase="not the passphrase").returncode, 0)

        restored = self.backup("-Import", str(file), "-Name", second)
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(release_key.public_key(second), original)
        key, signature = release_key.sign(b"signed by the restored key", second)
        self.assertTrue(release_signature.confirms(original, b"signed by the restored key", signature))
        # The restored key can be backed up again, and it is still a signing key.
        again = folder / "again.p8"
        self.assertEqual(self.backup("-Export", str(again), "-Name", second).returncode, 0)

        # Nothing is overwritten, nothing lands in the project, and a short passphrase is not enough.
        self.assertNotEqual(self.backup("-Import", str(file), "-Name", second).returncode, 0)
        self.assertNotEqual(self.backup("-Export", str(file), "-Name", first).returncode, 0)
        self.assertEqual(file.read_bytes(), data)
        inside = Path(release_key.ROOT) / "build" / "never-a-key-backup.p8"
        self.assertNotEqual(self.backup("-Export", str(inside), "-Name", first).returncode, 0)
        self.assertFalse(inside.exists())
        short = folder / "short.p8"
        self.assertNotEqual(self.backup("-Export", str(short), "-Name", first, passphrase="short").returncode, 0)
        self.assertFalse(short.exists())
        self.assertNotEqual(self.backup("-Export", str(folder / "other.p8"), "-Name", "SomeOtherKey").returncode, 0)

    def test_the_release_keys_backup_passphrase_can_only_be_typed(self):
        source = self.BACKUP.read_text(encoding="utf-8")
        # No parameter carries a passphrase, and the one unattended source is tied to test key names.
        parameters = source[source.index("param("):source.index("$ErrorActionPreference")]
        self.assertNotRegex(parameters, "(?i)pass|secret|password")
        uses = [line for line in source.splitlines() if "LUVBOX_TEST_KEY_PASSPHRASE" in line]
        self.assertEqual(len(uses), 2)
        self.assertIn("$Name.StartsWith($TestPrefix) -and", uses[0])
        self.assertLess(source.index(uses[0]), source.index("Read-Host -AsSecureString"))
        # The only keys it ever removes are the short-lived ones it makes to read a backup.
        self.assertEqual(source.count(".Delete()"), 1)
        self.assertLess(source.index("if ($trial.StartsWith($TestPrefix)"), source.index(".Delete()"))

    def test_the_tool_takes_only_the_release_key_or_a_test_key_and_cannot_write_or_remove_the_release_key(self):
        for action in ("-Public", "-Delete"):
            with self.assertRaises(release_key.KeyUnavailable):
                release_key._run(action, name="SomeOtherKey")
        source = (Path(release_key.SCRIPT)).read_text(encoding="utf-8")
        # Removing is for test keys only. Read from the source rather than tried on the release key.
        guard = source.index("if (-not $Name.StartsWith($TestPrefix))")
        self.assertLess(guard, source.index("$key.Delete()"))
        self.assertEqual(source.count(".Delete()"), 1)
        # Nothing in the tool can write the private part out.
        for word in ("Export(", "ExportParameters($true)", "ExportPkcs8", "ExportEncryptedPkcs8", "Set-Content",
                     "Out-File", "WriteAllBytes"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
