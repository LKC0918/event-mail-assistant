import unittest
from scripts.check_public_tree import categories


class PublicAuditTests(unittest.TestCase):
    def test_example_addresses_allowed(self):
        self.assertEqual(categories('you@example.com https://events.example.com/events/1'),set())

    def test_personal_path_rejected(self):
        value='/'+'Users'+'/'+'example-person'+'/workspace'
        self.assertIn('personal_path',categories(value))

    def test_nonexample_contact_rejected(self):
        value='person'+'@'+'private-mail'+'.invalid'
        self.assertIn('nonexample_email',categories(value))

    def test_credential_pattern_rejected_without_echo(self):
        value='gh'+'p_'+'x'*32
        self.assertEqual(categories(value),{'credential_pattern'})


if __name__=='__main__':unittest.main()
