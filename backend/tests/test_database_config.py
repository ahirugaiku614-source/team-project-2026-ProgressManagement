import importlib
import os
import unittest
from unittest.mock import patch

from sqlalchemy.engine import make_url

with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}):
    database = importlib.import_module("app.db.database")


class DatabaseConfigTests(unittest.TestCase):
    def test_explicit_database_url_takes_priority(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}, clear=True):
            self.assertEqual(database.get_database_url(), "sqlite://")

    def test_postgres_variables_and_special_characters(self):
        values = {
            "POSTGRES_USER": "test@user", "POSTGRES_PASSWORD": "test@:/#%$ password",
            "POSTGRES_DB": "test_database",
        }
        with patch.dict(os.environ, values, clear=True):
            url = database.get_database_url()
            self.assertEqual(url.drivername, "postgresql+psycopg2")
            self.assertEqual(url.host, "db")
            self.assertEqual(url.port, 5432)
            self.assertEqual(url.database, values["POSTGRES_DB"])
            parsed = make_url(url.render_as_string(hide_password=False))
            self.assertEqual(parsed.username, values["POSTGRES_USER"])
            self.assertEqual(parsed.password, values["POSTGRES_PASSWORD"])

    def test_local_host_and_port_can_be_configured(self):
        with patch.dict(os.environ, {
            "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test-only",
            "POSTGRES_DB": "test_db", "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "55439",
        }, clear=True):
            url = database.get_database_url()
            self.assertEqual((url.host, url.port), ("127.0.0.1", 55439))

    def test_missing_or_empty_required_configuration_fails_without_secrets(self):
        for missing in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            for value in (None, ""):
                with self.subTest(missing=missing, value=value):
                    config = {"POSTGRES_USER": "test", "POSTGRES_PASSWORD": "private-test-value", "POSTGRES_DB": "test_db"}
                    if value is None:
                        config.pop(missing)
                    else:
                        config[missing] = value
                    with patch.dict(os.environ, config, clear=True):
                        with self.assertRaises(RuntimeError) as error:
                            database.get_database_url()
                    self.assertIn(missing, str(error.exception))
                    self.assertNotIn("private-test-value", str(error.exception))
