import os
import unittest
from unittest.mock import patch

from price07_rev2_runner import auto_refresh_enabled


class Price07Rev2AutostartTests(unittest.TestCase):
    def test_disabled_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(auto_refresh_enabled())

    def test_only_exact_opt_in_enables(self):
        for value in ('true', 'yes', '0', ' 1x '):
            with self.subTest(value=value), patch.dict(os.environ, {'PRICE07_REV2_AUTO_ENABLED': value}):
                self.assertFalse(auto_refresh_enabled())
        with patch.dict(os.environ, {'PRICE07_REV2_AUTO_ENABLED': '1'}):
            self.assertTrue(auto_refresh_enabled())
