from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest

from sub2easy.intake import IntakeError, main, normalize_totp_secret, parse_batch


SEED = "JBSWY3DPEHPK3PXP"  # Public synthetic test material, not a real account.


def line(account="demo@example.invalid", password="DEMO-password", seed=SEED):
    return f"{account}----{password}----{seed}"


class IntakeTests(unittest.TestCase):
    def test_one_per_line_bom_crlf_and_blank_lines(self):
        batch = parse_batch("\ufeff" + line() + "\r\n\r\n" + line("two@example.invalid") + "\r\n")
        self.assertEqual([m.line for m in batch.materials], [1, 3])
        self.assertFalse(batch.issues)

    def test_escaped_email_and_password_kept_exactly(self):
        password = "  DEMO----pass\\@word  "
        material = parse_batch(line(" Demo\\@Example.Invalid ", password)).materials[0]
        self.assertEqual(material.account, "demo@example.invalid")
        self.assertEqual(material.password, password)

    def test_totp_normalized_but_password_not_normalized(self):
        material = parse_batch(line(seed=SEED.lower())).materials[0]
        self.assertEqual(material.totp_secret, SEED)

    def test_exact_duplicates_collapsed(self):
        batch = parse_batch(line() + "\n" + line("DEMO@EXAMPLE.INVALID"))
        self.assertEqual(len(batch.materials), 1)
        self.assertEqual(batch.duplicates, (2,))

    def test_conflicting_materials_reject_all_matching_rows(self):
        batch = parse_batch("\n".join([line(), line(password="changed"), line("other@example.invalid")]))
        self.assertEqual([m.line for m in batch.materials], [3])
        self.assertEqual([i.line for i in batch.issues], [1, 2])
        self.assertTrue(all(i.code == "CONFLICTING_LOGIN_MATERIAL" for i in batch.issues))

    def test_email_aliases_not_merged(self):
        batch = parse_batch("\n".join(line(a) for a in [
            "demo@example.invalid", "demo+tag@example.invalid", "de.mo@example.invalid",
        ]))
        self.assertEqual(len(batch.materials), 3)

    def test_bad_rows_report_line_and_code_only(self):
        batch = parse_batch("\n".join(["SECRET_BAD_RAW_INPUT", line(password=""), line(seed="123456")]))
        self.assertEqual(len(batch.issues), 3)
        self.assertEqual(len(batch.materials), 0)
        self.assertNotIn("SECRET_BAD_RAW_INPUT", json.dumps(batch.preview()))

    def test_no_login_secrets_in_preview_or_repr(self):
        batch = parse_batch(line())
        result = json.dumps(batch.preview()) + repr(batch) + repr(batch.materials[0])
        for secret in ["demo@example.invalid", "DEMO-password", SEED]:
            self.assertNotIn(secret, result)

    def test_invalid_base32_or_six_digit_code(self):
        for value in ["123456", "A43INVALID0", SEED + "=", "otpauth://totp/test", "AAAAAAAAAAAAAAAAA"]:
            with self.subTest(value=value), self.assertRaises(IntakeError):
                normalize_totp_secret(value)

    def test_size_and_count_limits(self):
        for value in ["x" * (2 * 1024 * 1024 + 1), "\n" * 10001]:
            with self.assertRaises(IntakeError):
                parse_batch(value)

    def test_demo_cli_does_not_echo_material(self):
        fixture = Path(__file__).resolve().parents[1] / "examples/logins.txt"
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main([str(fixture)]), 0)
        self.assertEqual(json.loads(out.getvalue())["accepted"], 2)
        self.assertNotIn("DEMO-password", out.getvalue())


if __name__ == "__main__":
    unittest.main()
