from pathlib import Path

import pytest

from ibkr_ato.config import DataDir, Settings, load_settings

EXAMPLE = Path(__file__).parents[1] / "ibkr-ato.example.toml"


def test_example_settings_file_loads_with_defaults():
    assert load_settings(EXAMPLE) == Settings()


def test_cli_overrides_file_and_none_is_ignored(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[defaults]\nmargin_foreign_share = 0.5\ninterest_source = "au"\n')
    s = load_settings(p, {"margin_foreign_share": 0.25, "interest_source": None})
    assert (s.margin_foreign_share, s.interest_source) == (0.25, "au")


@pytest.mark.parametrize("text", ['[defaults]\nmystery = 1\n',
                                  '[defaults]\nmargin_foreign_share = 2\n',
                                  '[defaults]\nreturn_of_capital = "maybe"\n',
                                  '[defaults]\nprior_losses = -1\n'])
def test_bad_settings_rejected(tmp_path, text):
    p = tmp_path / "s.toml"
    p.write_text(text)
    with pytest.raises((ValueError, TypeError)):
        load_settings(p)


def test_data_dir_needs_existing_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("IBKR_ATO_DATA", raising=False)
    with pytest.raises(ValueError):
        DataDir.from_arg_or_env(None)
    with pytest.raises(ValueError):
        DataDir.from_arg_or_env(str(tmp_path / "missing"))
    assert DataDir.from_arg_or_env(str(tmp_path)).out(2026).name == "FY2026"
