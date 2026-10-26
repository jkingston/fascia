"""Public CLI dispatch preserves arguments and never invokes a shell."""

import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "fascia_cli", Path(__file__).with_name("fascia.py")
)
fascia = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fascia)


class Cli(unittest.TestCase):
    def test_dispatch_preserves_arguments(self):
        for command, path in fascia.COMMANDS.items():
            self.assertEqual(path, "/usr/local/bin/fascia-" + command)
            with mock.patch.object(fascia.os, "execv") as execute:
                fascia.main([command, "a path with spaces", "$(literal)"])
            execute.assert_called_once_with(
                path, [path, "a path with spaces", "$(literal)"]
            )

    def test_help_and_unknown_command_never_execute(self):
        with (
            mock.patch.object(fascia.os, "execv") as execute,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            fascia.main(["--help"])
            self.assertIn("Fascia", output.getvalue())
            with self.assertRaisesRegex(SystemExit, "Unknown command"):
                fascia.main(["shell"])
            execute.assert_not_called()

    def test_missing_device_tools_are_explained(self):
        with (
            mock.patch.object(fascia.os, "execv", side_effect=FileNotFoundError),
            self.assertRaisesRegex(SystemExit, "runs on the Fascia device"),
        ):
            fascia.main(["setup"])


if __name__ == "__main__":
    unittest.main()
