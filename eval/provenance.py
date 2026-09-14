"""Source identity in a checkout or an image without Git installed."""
import os
import subprocess


def source_revision():
    try:
        return subprocess.check_output(['git','rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
    except (FileNotFoundError,subprocess.CalledProcessError):
        return os.getenv('SOURCE_REVISION','unknown')


def dirty_worktree():
    try:
        return bool(subprocess.check_output(['git','status','--porcelain'],text=True,stderr=subprocess.DEVNULL).strip())
    except (FileNotFoundError,subprocess.CalledProcessError):
        return None
