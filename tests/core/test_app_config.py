"""The machine config loader (~/.auryn/config.toml -> Backend). No FalkorDB needed."""

from __future__ import annotations

from auryn.core.app_config import load_backend


def test_defaults_to_local_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))  # dir need not even exist
    assert load_backend().mode == "local"


def test_reads_remote_backend(tmp_path, monkeypatch):
    home = tmp_path / "cg"
    home.mkdir()
    (home / "config.toml").write_text(
        '[backend]\nmode = "remote"\nhost = "db.example"\nport = 6400\n'
        'username = "u"\npassword = "p"\nssl = true\n'
    )
    monkeypatch.setenv("AURYN_HOME", str(home))
    b = load_backend()
    assert (b.mode, b.host, b.port, b.username, b.password, b.ssl) == (
        "remote",
        "db.example",
        6400,
        "u",
        "p",
        True,
    )


def test_unknown_mode_falls_back_to_local(tmp_path, monkeypatch):
    home = tmp_path / "cg"
    home.mkdir()
    (home / "config.toml").write_text('[backend]\nmode = "bogus"\n')
    monkeypatch.setenv("AURYN_HOME", str(home))
    assert load_backend().mode == "local"


def test_save_load_roundtrip_with_manifest_and_perms(tmp_path, monkeypatch):
    import os
    import stat

    from auryn.core.app_config import AgentRegistration, Backend, Config, load_config, save_config
    from auryn.core.home import config_path

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))  # dir created by save
    cfg = Config(
        backend=Backend(mode="remote", host="db", port=6400, username="u", password="p", ssl=True),
        agents=(AgentRegistration(name="claude-code", scope="user", server_name="auryn", skill_path="/s"),),
    )
    save_config(cfg)
    got = load_config()
    assert got.backend == cfg.backend
    assert got.agents == cfg.agents
    assert stat.S_IMODE(os.stat(config_path()).st_mode) == 0o600  # may hold a password


# -- hook enable/disable persistence ---------------------------------------------------------


def test_hooks_disabled_roundtrip_install_wide_and_per_repo(tmp_path, monkeypatch):
    from auryn.core.app_config import Config, RepoRegistration, load_config, save_config

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    cfg = Config(
        disabled_hooks=("link-tests",),
        repos=(RepoRegistration(local_dir="/work/acme", disabled_hooks=("sample-hook",)),),
    )
    save_config(cfg)
    got = load_config()
    assert got.disabled_hooks == ("link-tests",)
    assert got.repos[0].disabled_hooks == ("sample-hook",)


def test_effective_disabled_hooks_unions_installwide_and_per_repo(tmp_path, monkeypatch):
    from auryn.core.app_config import Config, RepoRegistration, effective_disabled_hooks, save_config

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    save_config(Config(
        disabled_hooks=("global-off",),
        repos=(RepoRegistration(local_dir="/work/acme", disabled_hooks=("repo-off",)),),
    ))
    assert effective_disabled_hooks("/work/acme") == {"global-off", "repo-off"}
    assert effective_disabled_hooks("/work/other") == {"global-off"}   # unknown repo -> install-wide only
    assert effective_disabled_hooks(None) == {"global-off"}


def test_auto_embed_roundtrip_and_setter(tmp_path, monkeypatch):
    from auryn.core.app_config import load_config, set_auto_embed

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    assert load_config().auto_embed is False          # default: off, absent from the file
    set_auto_embed(True)
    assert load_config().auto_embed is True            # persisted under [embeddings] auto
    set_auto_embed(False)
    assert load_config().auto_embed is False


def test_set_hook_disabled_install_wide(tmp_path, monkeypatch):
    from auryn.core.app_config import load_config, set_hook_disabled

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    set_hook_disabled("link-tests", True)
    assert load_config().disabled_hooks == ("link-tests",)
    set_hook_disabled("link-tests", False)   # re-enable removes it
    assert load_config().disabled_hooks == ()


def test_set_hook_disabled_per_repo_upserts_registration(tmp_path, monkeypatch):
    from auryn.core.app_config import load_config, set_hook_disabled

    monkeypatch.setenv("AURYN_HOME", str(tmp_path / "cg"))
    set_hook_disabled("sample-hook", True, local_dir="/work/acme")
    cfg = load_config()
    reg = next(r for r in cfg.repos if r.local_dir == "/work/acme")
    assert reg.disabled_hooks == ("sample-hook",)
    assert cfg.disabled_hooks == ()  # per-repo only, not install-wide
    set_hook_disabled("sample-hook", False, local_dir="/work/acme")
    reg = next(r for r in load_config().repos if r.local_dir == "/work/acme")
    assert reg.disabled_hooks == ()
