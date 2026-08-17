"""Runtime configuration, loaded from environment variables / .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STOREPILOT_", env_file=".env", extra="ignore")

    # Google Play — service account JSON key
    google_credentials: Path | None = None
    # GCS bucket holding Play stats/earnings CSV exports. The id varies by account age:
    # "pubsite_prod_rev_<n>" on older accounts, "pubsite_prod_<accountId>" on newer ones.
    google_reports_bucket: str | None = None

    # App Store Connect — API key (.p8) + identifiers
    asc_key_path: Path | None = None
    asc_key_id: str | None = None
    asc_issuer_id: str | None = None
    # Required by the salesReports endpoint; found in App Store Connect -> Payments and Financial Reports
    asc_vendor_number: str | None = None

    # Local blob cache for store reports (past months are immutable, so cached forever)
    cache_dir: Path | None = None
    cache_enabled: bool = True

    @property
    def google_play_enabled(self) -> bool:
        return self.google_credentials is not None

    @property
    def app_store_enabled(self) -> bool:
        return all([self.asc_key_path, self.asc_key_id, self.asc_issuer_id])

    @property
    def resolved_cache_dir(self) -> Path:
        if self.cache_dir is not None:
            return self.cache_dir.expanduser()
        return Path.home() / ".storepilot" / "cache"


settings = Settings()
