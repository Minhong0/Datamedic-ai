from config import settings


def test_settings_defaults():
    assert settings.llm_provider in ("claude", "openai")
    assert settings.standard_match_threshold == 0.8
    assert settings.llm_confidence_min == 0.6
