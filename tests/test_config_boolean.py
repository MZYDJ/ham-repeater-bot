#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯播报分支：布尔配置数值兼容回归测试。"""
import unittest
import direct_announce


class BooleanConfigCoercionTests(unittest.TestCase):
    def coerce(self, value, default=False):
        return direct_announce._coerce_config_value(value, default, ("test", "enabled"))

    def test_numeric_zero_and_one(self):
        self.assertIs(self.coerce(0), False)
        self.assertIs(self.coerce(1), True)
        self.assertIs(self.coerce(0.0), False)
        self.assertIs(self.coerce(1.0), True)

    def test_native_booleans_and_legacy_strings(self):
        self.assertIs(self.coerce(False), False)
        self.assertIs(self.coerce(True), True)
        self.assertIs(self.coerce("false", True), False)
        self.assertIs(self.coerce("true", False), True)
        self.assertIs(self.coerce("0"), False)
        self.assertIs(self.coerce("1"), True)

    def test_other_numbers_are_rejected(self):
        self.assertIs(self.coerce(2, False), False)
        self.assertIs(self.coerce(-1, True), True)


if __name__ == "__main__":
    unittest.main()
