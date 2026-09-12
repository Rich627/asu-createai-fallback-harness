import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from asu import winservice
NS = {"t": winservice.NAMESPACE}


def parse(xml):
    return ET.fromstring(xml)


class ArgumentQuotingTests(unittest.TestCase):
    def test_paths_with_spaces_are_quoted(self):
        line = winservice.argument_line(r"C:\Program Files\asu\claude_daemon.py", ["--port", "41118"])
        self.assertIn('"C:\\Program Files\\asu\\claude_daemon.py"', line)
        self.assertTrue(line.endswith("--port 41118"))

    def test_plain_arguments_are_left_bare(self):
        self.assertEqual(winservice.quote("--port"), "--port")

    def test_empty_argument_still_occupies_a_slot(self):
        self.assertEqual(winservice.quote(""), '""')

    def test_model_ids_with_slashes_survive(self):
        self.assertEqual(winservice.quote("aws/claude5_opus"), "aws/claude5_opus")


class TaskXmlTests(unittest.TestCase):
    def build(self):
        arguments = winservice.argument_line(r"C:\asu\claude_daemon.py",
                                             ["--port", "41118", "--log", r"C:\logs\b.log"])
        return winservice.task_xml(r"C:\Python\pythonw.exe", arguments, "ASU bridge",
                                   user="HOST\\rich")

    def test_is_well_formed(self):
        parse(self.build())

    def test_declares_utf16_because_schtasks_requires_unicode(self):
        self.assertIn('encoding="UTF-16"', self.build())

    def test_runs_at_logon_without_admin(self):
        root = parse(self.build())
        self.assertIsNotNone(root.find(".//t:LogonTrigger", NS))
        self.assertEqual(root.find(".//t:RunLevel", NS).text, "LeastPrivilege")

    def test_restarts_on_failure_like_keepalive(self):
        root = parse(self.build())
        restart = root.find(".//t:RestartOnFailure", NS)
        self.assertIsNotNone(restart)
        self.assertEqual(restart.find("t:Interval", NS).text, "PT1M")

    def test_has_no_execution_time_limit(self):
        """The Task Scheduler default stops a task after 72 hours, which would silently end the
        bridge and leave the client pointed at a dead port."""
        root = parse(self.build())
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", NS).text, "PT0S")

    def test_is_hidden_and_survives_battery_and_idle(self):
        root = parse(self.build())
        self.assertEqual(root.find(".//t:Hidden", NS).text, "true")
        self.assertEqual(root.find(".//t:StopIfGoingOnBatteries", NS).text, "false")
        self.assertEqual(root.find(".//t:DisallowStartIfOnBatteries", NS).text, "false")
        self.assertEqual(root.find(".//t:StopOnIdleEnd", NS).text, "false")

    def test_command_and_arguments_are_carried_through(self):
        root = parse(self.build())
        self.assertEqual(root.find(".//t:Command", NS).text, r"C:\Python\pythonw.exe")
        self.assertIn("--log", root.find(".//t:Arguments", NS).text)

    def test_user_metadata_is_escaped_not_injected(self):
        xml = winservice.task_xml("py.exe", "a", "desc", user='HOST\\a&b<c>"d"')
        root = parse(xml)
        self.assertEqual(root.find(".//t:Principal/t:UserId", NS).text, 'HOST\\a&b<c>"d"')

    def test_written_file_is_utf16_with_a_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "task.xml"
            winservice.write_task_xml(path, self.build())
            raw = path.read_bytes()
            self.assertIn(raw[:2], (b"\xff\xfe", b"\xfe\xff"))
            parse(raw.decode("utf-16"))


class PlatformGuardTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "the guard is what we are testing")
    def test_schtasks_calls_refuse_off_windows(self):
        from asu.createai import BridgeError
        for call in (lambda: winservice.task_exists("x"),
                     lambda: winservice.create_task("x", Path("y")),
                     lambda: winservice.start_task("x")):
            with self.subTest(call=call), self.assertRaises(BridgeError):
                call()


@unittest.skipUnless(sys.platform == "win32", "needs a real Task Scheduler")
class RealSchtasksTests(unittest.TestCase):
    """Runs only on Windows, where CI exercises the actual schtasks round trip."""

    NAME = "ASU Bridge Test Task"

    def tearDown(self):
        winservice.delete_task(self.NAME)

    def test_create_query_delete_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.xml"
            arguments = winservice.argument_line(Path(sys.executable).with_name("python.exe"), ["-V"])
            winservice.write_task_xml(path, winservice.task_xml(sys.executable, arguments,
                                                               "ASU bridge test task"))
            self.assertFalse(winservice.task_exists(self.NAME))
            winservice.create_task(self.NAME, path)
            self.assertTrue(winservice.task_exists(self.NAME))
            winservice.delete_task(self.NAME)
            self.assertFalse(winservice.task_exists(self.NAME))


if __name__ == "__main__":
    unittest.main()


class InstallerTaskDefinitionTests(unittest.TestCase):
    """The generators are exercised through the real installers, so a wrong daemon filename or a
    dropped option is caught on any platform rather than only on a Windows desktop."""

    class Args:
        environment = "production"
        model = "auto"
        primary = "chatgpt"
        port = 41118

    def definition(self, module):
        return module.task_definition(self.Args())

    def test_claude_task_launches_the_claude_daemon_with_its_log(self):
        import setup_claude_windows
        root = parse(self.definition(setup_claude_windows))
        arguments = root.find(".//t:Arguments", NS).text
        self.assertIn("claude_daemon.py", arguments)
        self.assertIn("--log", arguments)
        self.assertIn("--port 41118", arguments)
        self.assertNotIn("--primary", arguments)

    def test_codex_task_launches_the_codex_daemon_with_its_primary(self):
        import setup_codex_windows
        root = parse(self.definition(setup_codex_windows))
        arguments = root.find(".//t:Arguments", NS).text
        self.assertIn("codex_daemon.py", arguments)
        self.assertIn("--primary chatgpt", arguments)
        self.assertIn("--log", arguments)

    def test_the_two_tasks_do_not_share_a_name_or_a_log(self):
        import setup_claude_windows
        import setup_codex_windows
        self.assertNotEqual(setup_claude_windows.TASK, setup_codex_windows.TASK)
        self.assertNotEqual(setup_claude_windows.LOG, setup_codex_windows.LOG)
        self.assertNotEqual(setup_claude_windows.TASK_XML, setup_codex_windows.TASK_XML)

    def test_task_arguments_carry_only_known_non_secret_options(self):
        """A task definition is a file on disk the user can read, so the token must never be an
        argument — the service reads it from the credential store. Comparing the flag set exactly
        means anyone adding a --token here fails this test."""
        import setup_claude_windows
        import setup_codex_windows
        expected = {
            setup_claude_windows: {"--environment", "--model", "--port", "--log"},
            setup_codex_windows: {"--environment", "--model", "--primary", "--port", "--log"},
        }
        for module, allowed in expected.items():
            with self.subTest(module=module.__name__):
                root = parse(self.definition(module))
                arguments = root.find(".//t:Arguments", NS).text
                flags = {word for word in arguments.split() if word.startswith("--")}
                self.assertEqual(flags, allowed)
