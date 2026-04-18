from tests.cli.test_cli_init import _make_cli


def test_usagefooter_command_toggles_cli_state():
    cli = _make_cli()

    assert cli.reply_usage_footer_enabled is False

    cli.process_command("/usagefooter on")
    assert cli.reply_usage_footer_enabled is True

    cli.process_command("/usagefooter off")
    assert cli.reply_usage_footer_enabled is False
