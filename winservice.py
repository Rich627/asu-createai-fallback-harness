"""Registering the bridges as Windows scheduled tasks — the LaunchAgent equivalent.

`schtasks /Create /TR` cannot express what a LaunchAgent gives for free, so the task is defined
as XML instead: a logon trigger (RunAtLoad), RestartOnFailure (KeepAlive), no execution time
limit (the default kills a task after 72 hours), and Hidden so nothing flashes on screen.

Unlike macOS there is no Keychain ACL to respect here — a Credential Manager entry belongs to
the user, not to a trusted binary — so the interpreter may change between installs without
prompting anyone. `pythonw.exe` is preferred purely so no console window appears; because it has
no console, the daemons take `--log` and write their own file instead of being redirected.

The generators are pure and unit-tested on every platform; only the schtasks calls need Windows.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from createai import BridgeError

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def require_windows():
    if sys.platform != "win32":
        raise BridgeError("This installer supports Windows only.")


def interpreter():
    """pythonw.exe when it sits beside this interpreter, so the service runs without a window."""
    current = Path(sys.executable)
    windowless = current.with_name("pythonw.exe")
    if windowless.exists():
        return str(windowless)
    return str(current)


def principal():
    """DOMAIN\\user when the domain is known, otherwise the bare user name."""
    import getpass
    import os
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def quote(argument):
    """Quote one argument for the Arguments element of a task action."""
    text = str(argument)
    if not text:
        return '""'
    return f'"{text}"' if (" " in text or "\t" in text) else text


def argument_line(script, options):
    return " ".join([quote(script)] + [quote(item) for item in options])


def task_xml(command, arguments, description, user=None):
    """The full task definition. Values are XML-escaped; callers pass raw strings."""
    user = user or principal()
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{NAMESPACE}">
  <RegistrationInfo>
    <Author>{escape(user)}</Author>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>99</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
'''


def write_task_xml(path, xml):
    """schtasks reads the definition as Unicode, so the file is UTF-16 with a BOM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(xml.encode("utf-16"))


def _schtasks(*arguments):
    return subprocess.run(["schtasks", *arguments], check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def task_exists(name):
    require_windows()
    return _schtasks("/Query", "/TN", name).returncode == 0


def create_task(name, xml_path):
    require_windows()
    result = _schtasks("/Create", "/TN", name, "/XML", str(xml_path), "/F")
    if result.returncode:
        raise BridgeError(f"Could not register the scheduled task {name}: "
                          f"{(result.stdout or '').strip()}")


def start_task(name):
    require_windows()
    result = _schtasks("/Run", "/TN", name)
    if result.returncode:
        raise BridgeError(f"Could not start the scheduled task {name}: "
                          f"{(result.stdout or '').strip()}")


def stop_task(name):
    """Best effort: /End fails harmlessly when the task is not running."""
    require_windows()
    _schtasks("/End", "/TN", name)


def delete_task(name):
    """Best effort: /Delete fails harmlessly when the task does not exist."""
    require_windows()
    _schtasks("/Delete", "/TN", name, "/F")
