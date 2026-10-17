import subprocess
import unittest
from unittest.mock import patch
import tray_launcher


class TrayLauncherTests(unittest.TestCase):
    def test_tray_is_detached_and_breaks_away_from_terminal_job(self):
        with patch.object(tray_launcher.subprocess,'Popen') as spawn:
            tray_launcher.launch()
        flags=spawn.call_args.kwargs['creationflags']
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(flags & subprocess.DETACHED_PROCESS)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertTrue(spawn.call_args.args[0][0].endswith('pythonw.exe'))
        self.assertEqual(spawn.call_args.kwargs['stdin'],subprocess.DEVNULL)

    def test_optional_status_is_an_argument_not_a_shell_command(self):
        with patch.object(tray_launcher.subprocess,'Popen') as spawn:
            tray_launcher.launch(show_status=True)
        self.assertIn('--status-on-launch',spawn.call_args.args[0])
        self.assertFalse(spawn.call_args.kwargs.get('shell',False))


if __name__=='__main__':unittest.main()
