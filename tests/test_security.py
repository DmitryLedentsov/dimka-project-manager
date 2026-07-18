import unittest

from dpm.security import hash_password, verify_password


class SecurityTests(unittest.TestCase):
    def test_password_roundtrip(self) -> None:
        encoded = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong", encoded))

    def test_empty_password_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            hash_password("")


if __name__ == "__main__":
    unittest.main()
