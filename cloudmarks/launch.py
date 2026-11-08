"""Start rclone for a user service with an environment CloudMarks controls.

rclone reads ``RCLONE_*`` variables (for example ``RCLONE_CONFIG_<REMOTE>_*``)
even when ``--config`` names a private file, so a variable in the user
manager's environment could silently change the remote a service mounts.
systemd cannot unset variables by prefix, so services start rclone through
this launcher. It replaces itself with rclone (same PID, so ``Type=notify``
still works) after removing every ``RCLONE_*`` variable.
"""

import os

from .model import ConfigurationError
from .onedrive import clean_environment


def exec_clean(argv, *, execve=os.execve):
    if not argv or not os.path.isabs(argv[0]) or os.path.basename(argv[0]) != "rclone":
        raise ConfigurationError("rclone-exec needs the absolute path of the rclone program")
    execve(argv[0], list(argv), clean_environment())
