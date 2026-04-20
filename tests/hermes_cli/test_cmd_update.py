"""Tests for cmd_update on the standalone napcat update branch."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.main import PROJECT_ROOT, cmd_update


def _make_run_side_effect(branch="napcat", commit_count="0", verify_ok=True):
    """Build a side_effect function for subprocess.run that simulates git commands."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace()


class TestCmdUpdateStandaloneBranch:
    """cmd_update should always align the checkout to the standalone napcat ref."""

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_switches_feature_branch_to_napcat(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(branch="fix/stoicneko", commit_count="3")

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        fetch_cmds = [c for c in commands if c.startswith("git fetch")]
        assert len(fetch_cmds) == 1
        assert "https://github.com/theTd/hermes-agent.git" in fetch_cmds[0]
        assert "refs/heads/napcat:refs/remotes/hermes-standalone/napcat" in fetch_cmds[0]

        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "refs/remotes/hermes-standalone/napcat" in rev_list_cmds[0]
        assert "origin/fix/stoicneko" not in rev_list_cmds[0]

        checkout_cmds = [c for c in commands if "checkout napcat" in c]
        assert len(checkout_cmds) == 1

        reset_cmds = [c for c in commands if "reset" in c and "--hard refs/remotes/hermes-standalone/napcat" in c]
        assert len(reset_cmds) == 1

        pull_cmds = [c for c in commands if "pull" in c]
        assert pull_cmds == []

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_uses_napcat_when_already_on_target_branch(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(branch="napcat", commit_count="2")

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        fetch_cmds = [c for c in commands if c.startswith("git fetch")]
        assert len(fetch_cmds) == 1
        assert "https://github.com/theTd/hermes-agent.git" in fetch_cmds[0]
        assert "refs/heads/napcat:refs/remotes/hermes-standalone/napcat" in fetch_cmds[0]

        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "refs/remotes/hermes-standalone/napcat" in rev_list_cmds[0]

        checkout_cmds = [c for c in commands if "checkout napcat" in c]
        assert checkout_cmds == []

        reset_cmds = [c for c in commands if "reset" in c and "--hard refs/remotes/hermes-standalone/napcat" in c]
        assert len(reset_cmds) == 1

    @patch("shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_update_already_up_to_date(
        self, mock_run, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(branch="napcat", commit_count="0")

        cmd_update(mock_args)

        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        reset_cmds = [c for c in commands if "reset" in c]
        assert reset_cmds == []

        pull_cmds = [c for c in commands if "pull" in c]
        assert pull_cmds == []

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_update_refreshes_repo_and_tui_node_dependencies(
        self, mock_run, mock_which, mock_args
    ):
        mock_which.side_effect = {"uv": "/usr/bin/uv", "npm": "/usr/bin/npm"}.get
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="1"
        )

        cmd_update(mock_args)

        npm_calls = [
            (call.args[0], call.kwargs.get("cwd"))
            for call in mock_run.call_args_list
            if call.args and call.args[0][0] == "/usr/bin/npm"
        ]

        # cmd_update runs npm commands in three locations:
        #   1. repo root  — slash-command / TUI bridge deps
        #   2. ui-tui/    — Ink TUI deps
        #   3. web/       — install + "npm run build" for the web frontend
        full_flags = [
            "/usr/bin/npm",
            "ci",
            "--silent",
            "--no-fund",
            "--no-audit",
            "--progress=false",
        ]
        assert npm_calls == [
            (full_flags, PROJECT_ROOT),
            (full_flags, PROJECT_ROOT / "ui-tui"),
            (["/usr/bin/npm", "ci", "--silent"], PROJECT_ROOT / "web"),
            (["/usr/bin/npm", "run", "build"], PROJECT_ROOT / "web"),
        ]

    def test_update_non_interactive_skips_migration_prompt(self, mock_args, capsys):
        """When stdin/stdout aren't TTYs, config migration prompt is skipped."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.config.get_missing_env_vars", return_value=["MISSING_KEY"]
        ), patch("hermes_cli.config.get_missing_config_fields", return_value=[]), patch(
            "hermes_cli.config.check_config_version", return_value=(1, 2)
        ), patch("hermes_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stdout.isatty.return_value = False
            mock_run.side_effect = _make_run_side_effect(branch="napcat", commit_count="1")

            cmd_update(mock_args)

            mock_input.assert_not_called()
            captured = capsys.readouterr()
            assert "Non-interactive session" in captured.out
