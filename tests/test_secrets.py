"""The secrets package: SecretValue, value-based redaction, masking."""

import copy
import json
import pickle
import unittest

from inkcast.libs.secrets import Redactor, SecretValue, Sealer, describe_keys, fingerprint, partly_mask
from inkcast.libs.secrets.redactor import is_sensitive_key


class SecretValueTests(unittest.TestCase):
    def test_never_prints_and_compares_in_constant_time(self):
        s = SecretValue("hunter2-hunter2")
        self.assertEqual(repr(s), "SecretValue('***')")
        self.assertNotIn("hunter2", f"{s} {s!r} {s!s} {[s]} {dict(k=s)} {s:>20}")
        self.assertNotIn("hunter2", "%s %r" % (s, s))
        self.assertEqual(s.reveal(), "hunter2-hunter2")
        self.assertEqual(s, SecretValue("hunter2-hunter2"))
        self.assertEqual(s, "hunter2-hunter2")                 # plain strings compare too (constant time)
        self.assertNotEqual(s, "hunter2-hunter3")
        self.assertNotEqual(s, 42)
        self.assertTrue(s)
        self.assertFalse(SecretValue(""))
        self.assertEqual(SecretValue(s).reveal(), s.reveal())   # idempotent wrap
        with self.assertRaises(TypeError):
            SecretValue(123)  # type: ignore[arg-type]

    def test_refuses_the_accidental_serialisations(self):
        s = SecretValue("hunter2-hunter2")
        with self.assertRaises(TypeError):
            json.dumps({"k": s})
        with self.assertRaises(TypeError):
            pickle.dumps(s)
        self.assertIs(copy.deepcopy(s), s)
        self.assertEqual(hash(s), hash(SecretValue("hunter2-hunter2")))

    def test_sealer_accepts_plain_or_wrapped_keys(self):
        a, b = Sealer("k" * 40), Sealer(SecretValue("k" * 40))
        self.assertEqual(b.unseal(a.seal({"api_key": "x"})), {"api_key": "x"})
        with self.assertRaisesRegex(RuntimeError, "INKCAST_SECRET_KEY changed"):
            Sealer("j" * 40).unseal(a.seal({"api_key": "x"}))


class RedactorTests(unittest.TestCase):
    def test_registered_values_are_masked_in_any_encoding(self):
        r = Redactor()
        r.register("p@ss/word+123")
        r.register("short")  # too short to register safely
        self.assertEqual(r.size, 2)  # raw and URL-encoded forms (the JSON form is the same here)
        r.register('quo"te-value')
        self.assertTrue(r.contains('{"pw": "quo\\"te-value"}'))  # the JSON-escaped form
        text = 'connect postgres://app:p@ss/word+123@db failed; q=p%40ss%2Fword%2B123'
        out = r.scrub(text)
        self.assertNotIn("p@ss", out)
        self.assertNotIn("p%40ss", out)
        self.assertIn("[REDACTED]", out)
        self.assertTrue(r.contains(text))
        self.assertFalse(r.contains("nothing here"))
        self.assertEqual(r.scrub("https://u:topsecretpw@host/x"), "https://u:[REDACTED]@host/x")  # even unregistered

    def test_register_mapping_takes_credential_leaves_only(self):
        r = Redactor()
        r.register_mapping({"username": "alice-in-wonderland", "password": "wonderland-42", "nested": {"api_key": "key-1234567"}},
                           raw='{"password": "wonderland-42"}')
        self.assertTrue(r.contains("wonderland-42"))
        self.assertTrue(r.contains("key-1234567"))
        self.assertFalse(r.contains("alice-in-wonderland"))

    def test_scrub_value_blanks_sensitive_keys_and_reduces_errors(self):
        r = Redactor()
        r.register("hunter2-hunter2")
        out = r.scrub_value({"password": "x", "client_secret": "", "note": "pw hunter2-hunter2", "err": ValueError("hunter2-hunter2"),
                             "secret": SecretValue("abc"), "items": ["hunter2-hunter2"]})
        self.assertEqual(out["password"], "[REDACTED]")
        self.assertEqual(out["client_secret"], "")
        self.assertEqual(out["note"], "pw [REDACTED]")
        self.assertEqual(out["err"], {"type": "ValueError", "message": "[REDACTED]"})
        self.assertEqual(out["secret"], "[REDACTED]")
        self.assertEqual(out["items"], ["[REDACTED]"])
        self.assertTrue(is_sensitive_key("OIDC_CLIENT_SECRET") and is_sensitive_key("x-vault-token"))
        self.assertTrue(is_sensitive_key("INKCAST_SECRET_KEY") and is_sensitive_key("token_hash") is False)
        self.assertFalse(is_sensitive_key("host") or is_sensitive_key("INKCAST_SECRET_KEY_FILE") or is_sensitive_key("INKCAST_SECRETS_DIR"))

    def test_remembers_a_bounded_number_of_values(self):
        r = Redactor()
        for i in range(300):
            r.register(f"value-number-{i:04d}")
        self.assertLessEqual(r.size, 200)
        self.assertTrue(r.contains("value-number-0299"))
        self.assertFalse(r.contains("value-number-0000"))


class MaskTests(unittest.TestCase):
    def test_describe_keys_shows_names_and_fingerprints_never_values(self):
        key = b"k" * 32
        lines = describe_keys({"INKCAST_SECRET_KEY": "s" * 48, "host": "db.internal", "username": "northwind-app",
                               "OIDC_CLIENT_SECRET": SecretValue("kc-secret-value")}, key)
        self.assertEqual(lines[0], f"INKCAST_SECRET_KEY=******** (48 chars, fp={fingerprint('s' * 48, key)})")
        self.assertEqual(lines[1], "host=db.internal")
        self.assertEqual(lines[2], "username=no*********pp")
        self.assertIn("(15 chars, fp=", lines[3])
        self.assertNotIn("kc-secret", " ".join(lines))
        self.assertNotEqual(fingerprint("a" * 20, key), fingerprint("a" * 20, b"j" * 32))  # key-dependent
        self.assertEqual(partly_mask("ab"), "**")
        self.assertEqual(partly_mask("abcdef"), "a*****")


if __name__ == "__main__":
    unittest.main()
