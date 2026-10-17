"""Launch the tray independently of a terminal/Codex process Job Object."""
import argparse
import subprocess
from tray_control import PROJECT


def launch(show_status=False):
    command = [str(PROJECT/'.venv-wechat/Scripts/pythonw.exe'), '-X', 'utf8', str(PROJECT/'tray_app.py')]
    if show_status:
        command.append('--status-on-launch')
    # pythonw hides the GUI runtime console. Break away from the launcher's
    # Job so ending a terminal/tool session cannot kill the tray with it.
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP |
             subprocess.CREATE_BREAKAWAY_FROM_JOB)
    return subprocess.Popen(command, cwd=PROJECT, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--show-status', action='store_true')
    args = parser.parse_args()
    try:
        launch(args.show_status)
    except OSError:
        raise SystemExit('Detached tray launch refused by Windows; launch the project shortcut from Explorer.') from None


if __name__=='__main__':main()
