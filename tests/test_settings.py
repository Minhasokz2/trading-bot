import settings


def test_defaults_and_toml_override(tmp_path):
    cfg = settings.load(tmp_path / "missing.toml")
    assert cfg["costs"]["fee"] == 0.001 and cfg["gates"]["min_trades"] == 20
    p = tmp_path / "settings.toml"
    p.write_text('[costs]\nfee = 0.00075\n[gates]\nmin_dsr = 0.8\n[risk]\naccount_usd = 2500\n')
    cfg2 = settings.load(p)
    assert cfg2["costs"]["fee"] == 0.00075 and cfg2["costs"]["slippage"] == 0.0005      # untouched keys keep defaults
    assert cfg2["gates"]["min_dsr"] == 0.8 and cfg2["gates"]["min_trades"] == 20
    assert cfg2["risk"]["account_usd"] == 2500 and cfg2["weights"] == settings.DEFAULTS["weights"]
    assert settings.DEFAULTS["gates"]["min_dsr"] == 0.90                                # defaults not mutated


def test_shipped_settings_file_matches_defaults():
    shipped = settings.load(settings.PATH)
    assert shipped == settings.DEFAULTS
