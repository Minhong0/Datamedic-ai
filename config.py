from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = "claude"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    odcloud_api_key: str = ""
    nts_mode: str = "mock"
    llm_model_claude: str = "claude-sonnet-4-6"
    llm_model_openai: str = "gpt-4o"

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_dry_run: bool = True

    data_dir: Path = BASE_DIR / "data"
    outputs_dir: Path = BASE_DIR / "outputs"
    cache_dir: Path = BASE_DIR / "outputs" / ".cache"
    standard_dir: Path = BASE_DIR / "data" / "standard"

    standard_match_threshold: float = 0.8
    llm_confidence_min: float = 0.6
    near_duplicate_threshold: float = 0.9
    outlier_iqr_factor: float = 1.5
    outlier_org_factor: float = 5.0
    llm_temperature: float = 0.0
    llm_max_retries: int = 2
    llm_cache_ttl_seconds: int = 0

    disabled_rules: str = ""

    @property
    def disabled_rule_ids(self) -> set[str]:
        """비활성 규칙 ID를 집합으로 반환."""
        return {r.strip() for r in self.disabled_rules.split(",") if r.strip()}


settings = Settings()
