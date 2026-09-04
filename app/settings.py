from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    environment: str
    data_dir: Path
    secrets_dir: Path
    config_dir: Path
    image_provider: str
    transcription_provider: str
    normalization_provider: str
    google_api_base_url: str
    google_api_key: str
    google_image_model: str
    google_transcription_model: str
    google_normalization_model: str
    google_timeout_seconds: float
    google_image_aspect_ratio: str
    google_image_size: str
    api_keys: tuple[str, ...]
    operator_access_key: str
    operator_session_ttl_minutes: int
    operator_cookie_name: str
    max_text_length: int
    max_audio_size: int
    supported_audio_types: tuple[str, ...]
    palette_snap_distance: float
    palette_cleanup_passes: int
    palette_generation_attempts: int
    requests_per_minute: int
    concurrent_requests_per_client: int
    allowed_hosts: tuple[str, ...]
    cors_origins: tuple[str, ...]
    staging_retention_hours: int
    input_retention_hours: int
    completed_retention_days: int
    in_progress_stale_minutes: int
    usage_retention_days: int
    usage_max_rows: int
    audit_retention_days: int
    audit_max_events: int
    audit_max_bytes: int
    backup_max_compressed_bytes: int
    backup_max_uncompressed_bytes: int
    backend_repository_url: str
    updater_catalog_token: str
    updater_socket_path: Path
    updater_control_token: str
    app_version: str

    @classmethod
    def from_env(cls) -> Settings:
        environment = os.getenv("MOONLI_ENV", "development").strip().lower()
        default_api_key = "dev-moonli-client-key" if environment != "production" else ""
        legacy_api_keys = os.getenv("MOONLI_API_KEYS", "")
        data_dir = Path(
            os.getenv("MOONLI_DATA_DIR", str(BASE_DIR / "data" / "v1"))
        ).resolve()
        settings = cls(
            environment=environment,
            data_dir=data_dir,
            secrets_dir=Path(
                os.getenv("MOONLI_SECRETS_DIR", str(data_dir / "secrets"))
            ).resolve(),
            config_dir=Path(os.getenv("MOONLI_CONFIG_DIR", str(BASE_DIR / "config"))).resolve(),
            image_provider=os.getenv("MOONLI_IMAGE_PROVIDER", "mock").strip().lower(),
            transcription_provider=os.getenv("MOONLI_TRANSCRIPTION_PROVIDER", "mock").strip().lower(),
            normalization_provider=os.getenv("MOONLI_NORMALIZATION_PROVIDER", "mock").strip().lower(),
            google_api_base_url=os.getenv(
                "MOONLI_GOOGLE_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
            ).rstrip("/"),
            google_api_key=os.getenv("MOONLI_GOOGLE_API_KEY", "").strip(),
            google_image_model=os.getenv("MOONLI_GOOGLE_IMAGE_MODEL", "").strip(),
            google_transcription_model=os.getenv("MOONLI_GOOGLE_TRANSCRIPTION_MODEL", "").strip(),
            google_normalization_model=os.getenv("MOONLI_GOOGLE_NORMALIZATION_MODEL", "").strip(),
            google_timeout_seconds=float(os.getenv("MOONLI_GOOGLE_TIMEOUT_SECONDS", "180")),
            google_image_aspect_ratio=os.getenv("MOONLI_GOOGLE_IMAGE_ASPECT_RATIO", "1:1").strip(),
            google_image_size=os.getenv("MOONLI_GOOGLE_IMAGE_SIZE", "1K").strip(),
            api_keys=_csv(
                "MOONLI_CLIENT_API_KEYS",
                legacy_api_keys or default_api_key,
            ),
            operator_access_key=os.getenv(
                "MOONLI_OPERATOR_ACCESS_KEY",
                "dev-moonli-operator-key-01" if environment != "production" else "",
            ).strip(),
            operator_session_ttl_minutes=int(
                os.getenv("MOONLI_OPERATOR_SESSION_TTL_MINUTES", "480")
            ),
            operator_cookie_name=os.getenv(
                "MOONLI_OPERATOR_COOKIE_NAME", "moonli_operator_session"
            ).strip(),
            max_text_length=int(os.getenv("MOONLI_MAX_TEXT_LENGTH", "12000")),
            max_audio_size=int(os.getenv("MOONLI_MAX_AUDIO_SIZE", str(20 * 1024 * 1024))),
            supported_audio_types=_csv(
                "MOONLI_SUPPORTED_AUDIO_TYPES",
                "audio/ogg,audio/mpeg,audio/wav,audio/mp4,audio/aac,audio/flac,video/webm,audio/webm",
            ),
            palette_snap_distance=float(os.getenv("MOONLI_PALETTE_SNAP_DISTANCE", "12")),
            palette_cleanup_passes=int(os.getenv("MOONLI_PALETTE_CLEANUP_PASSES", "1")),
            palette_generation_attempts=int(os.getenv("MOONLI_PALETTE_GENERATION_ATTEMPTS", "3")),
            requests_per_minute=int(os.getenv("MOONLI_REQUESTS_PER_MINUTE", "30")),
            concurrent_requests_per_client=int(os.getenv("MOONLI_CONCURRENT_REQUESTS_PER_CLIENT", "2")),
            allowed_hosts=_csv("MOONLI_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"),
            cors_origins=_csv("MOONLI_CORS_ORIGINS"),
            staging_retention_hours=int(os.getenv("MOONLI_STAGING_RETENTION_HOURS", "24")),
            input_retention_hours=int(os.getenv("MOONLI_INPUT_RETENTION_HOURS", "24")),
            completed_retention_days=int(os.getenv("MOONLI_COMPLETED_RETENTION_DAYS", "30")),
            in_progress_stale_minutes=int(os.getenv("MOONLI_IN_PROGRESS_STALE_MINUTES", "30")),
            usage_retention_days=int(os.getenv("MOONLI_USAGE_RETENTION_DAYS", "365")),
            usage_max_rows=int(os.getenv("MOONLI_USAGE_MAX_ROWS", "1000000")),
            audit_retention_days=int(os.getenv("MOONLI_AUDIT_RETENTION_DAYS", "30")),
            audit_max_events=int(os.getenv("MOONLI_AUDIT_MAX_EVENTS", "10000")),
            audit_max_bytes=int(os.getenv("MOONLI_AUDIT_MAX_BYTES", str(64 * 1024 * 1024))),
            backup_max_compressed_bytes=int(
                os.getenv("MOONLI_BACKUP_MAX_COMPRESSED_BYTES", str(128 * 1024 * 1024))
            ),
            backup_max_uncompressed_bytes=int(
                os.getenv("MOONLI_BACKUP_MAX_UNCOMPRESSED_BYTES", str(256 * 1024 * 1024))
            ),
            backend_repository_url=os.getenv(
                "MOONLI_BACKEND_REPOSITORY_URL",
                "https://github.com/psewdon1m-moonli/backend.git",
            ).strip(),
            updater_catalog_token=os.getenv("MOONLI_UPDATER_CATALOG_TOKEN", "").strip(),
            updater_socket_path=Path(
                os.getenv("MOONLI_UPDATER_SOCKET_PATH", "/run/exocortex/updater.sock")
            ),
            updater_control_token=os.getenv("MOONLI_UPDATER_CONTROL_TOKEN", "").strip(),
            app_version=os.getenv("MOONLI_VERSION", "0.1.0").strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("MOONLI_ENV must be development, test, or production")
        if self.image_provider not in {"mock", "google"}:
            raise ValueError("MOONLI_IMAGE_PROVIDER must be mock or google")
        if self.transcription_provider not in {"mock", "google"}:
            raise ValueError("MOONLI_TRANSCRIPTION_PROVIDER must be mock or google")
        if self.normalization_provider not in {"mock", "google"}:
            raise ValueError("MOONLI_NORMALIZATION_PROVIDER must be mock or google")
        if not self.api_keys:
            raise ValueError("At least one client API key must be configured")
        if not self.operator_access_key and self.environment != "production":
            raise ValueError("MOONLI_OPERATOR_ACCESS_KEY must be configured")
        if self.operator_session_ttl_minutes < 5 or not self.operator_cookie_name:
            raise ValueError("Operator session settings are invalid")
        if self.max_text_length <= 0 or self.max_audio_size <= 0:
            raise ValueError("Input limits must be positive")
        if (
            self.palette_snap_distance < 0
            or not 0 <= self.palette_cleanup_passes <= 3
            or self.palette_generation_attempts < 1
        ):
            raise ValueError("Palette validation settings are invalid")
        if self.requests_per_minute < 1 or self.concurrent_requests_per_client < 1:
            raise ValueError("Rate limit settings must be positive")
        if min(
            self.staging_retention_hours,
            self.input_retention_hours,
            self.completed_retention_days,
            self.in_progress_stale_minutes,
            self.usage_retention_days,
            self.usage_max_rows,
        ) <= 0:
            raise ValueError("Retention and stale-run settings must be positive")
        if min(self.audit_retention_days, self.audit_max_events, self.audit_max_bytes) <= 0:
            raise ValueError("Audit retention settings must be positive")
        if (
            self.backup_max_compressed_bytes <= 0
            or self.backup_max_uncompressed_bytes < self.backup_max_compressed_bytes
        ):
            raise ValueError("Backup size limits are invalid")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError("MOONLI_ALLOWED_HOSTS must contain explicit hosts")
        parsed_google_url = urlparse(self.google_api_base_url)
        if (
            parsed_google_url.scheme != "https"
            or not parsed_google_url.hostname
            or not (
                parsed_google_url.hostname == "googleapis.com"
                or parsed_google_url.hostname.endswith(".googleapis.com")
            )
            or parsed_google_url.username
            or parsed_google_url.password
            or parsed_google_url.query
            or parsed_google_url.fragment
        ):
            raise ValueError("MOONLI_GOOGLE_API_BASE_URL must be an HTTPS googleapis.com URL")
        if self.environment == "production":
            if (
                self.image_provider == "mock"
                or self.transcription_provider == "mock"
                or self.normalization_provider == "mock"
            ):
                raise ValueError("Mock providers are forbidden in production")
            if any(key.startswith("dev-") for key in self.api_keys):
                raise ValueError("Development API keys are forbidden in production")
            verifier_exists = (self.data_dir / "operator-auth.sqlite3").exists()
            if not self.operator_access_key and not verifier_exists:
                raise ValueError(
                    "MOONLI_OPERATOR_ACCESS_KEY is required for first production bootstrap"
                )
            if self.operator_access_key and self.operator_access_key.startswith("dev-"):
                raise ValueError("Development operator Access Keys are forbidden in production")
            if self.operator_access_key and len(self.operator_access_key) < 24:
                raise ValueError("Production operator Access Key must contain at least 24 characters")
            if any(
                secrets_equal(self.operator_access_key, key) for key in self.api_keys
            ):
                raise ValueError("Operator and client credentials must be different")
            if self.google_api_key:
                raise ValueError(
                    "MOONLI_GOOGLE_API_KEY is forbidden in production; configure it in the browser"
                )


def secrets_equal(left: str, right: str) -> bool:
    """Length-independent comparison used only for configuration validation."""
    import secrets

    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
