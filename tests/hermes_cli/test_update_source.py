import subprocess
from pathlib import Path
from unittest.mock import patch

from hermes_cli.update_source import fetch_standalone_update_ref


def test_fetch_standalone_update_ref_includes_repo_and_refspec(tmp_path: Path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("hermes_cli.update_source.subprocess.run", side_effect=fake_run):
        fetch_standalone_update_ref(["git"], tmp_path)

    assert seen["cmd"] == [
        "git",
        "fetch",
        "https://github.com/theTd/hermes-agent.git",
        "+refs/heads/napcat:refs/remotes/hermes-standalone/napcat",
    ]


def test_fetch_standalone_update_ref_quiet_adds_quiet_flag(tmp_path: Path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("hermes_cli.update_source.subprocess.run", side_effect=fake_run):
        fetch_standalone_update_ref(["git"], tmp_path, quiet=True)

    assert seen["cmd"] == [
        "git",
        "fetch",
        "--quiet",
        "https://github.com/theTd/hermes-agent.git",
        "+refs/heads/napcat:refs/remotes/hermes-standalone/napcat",
    ]
