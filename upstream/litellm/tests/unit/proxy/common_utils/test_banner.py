from litellm.proxy.common_utils.banner import LITELLM_BANNER, show_banner


def test_banner_names_the_distribution():
    lines = LITELLM_BANNER.splitlines()
    assert "noenterprise · sudo-ai.de" in lines[-1]
    # The subtitle stays as wide as the logo, so 80-column terminals do not wrap it.
    assert len({len(line) for line in lines}) == 1


def test_show_banner_prints_the_banner(capsys):
    show_banner()
    assert "noenterprise · sudo-ai.de" in capsys.readouterr().out
