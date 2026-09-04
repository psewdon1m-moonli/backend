from __future__ import annotations

from dataclasses import dataclass

from app.api.auth import ClientAuthenticator
from app.api.rate_limit import ClientRateLimiter
from app.domain.profiles import PipelineProfile, load_profiles
from app.observability.audit import AuditStore
from app.providers.factory import (
    create_image_generator,
    create_prompt_normalizer,
    create_transcriber,
)
from app.providers.image_generation.variants import (
    GoogleImageVariantGenerator,
    MockImageVariantGenerator,
)
from app.providers.prompt_translation import (
    GooglePromptTranslator,
    MockPromptTranslator,
)
from app.security.operator_auth import LoginRateLimiter, OperatorAuthStore
from app.services.backup import BackupManager
from app.services.generation_service import GenerationService
from app.services.google_key_validator import GoogleKeyValidator
from app.services.input_resolver import InputResolver
from app.services.outputs import RuntimeValidator
from app.services.pipeline3 import Pipeline3Service
from app.services.processing.palette_quantizer import PaletteQuantizer
from app.services.processing.palette_validator import PaletteValidator
from app.services.prompts import PromptBuilder
from app.services.updater_client import UpdaterClient
from app.settings import Settings
from app.storage.artifact_store import LocalArtifactStore
from app.storage.device_registry import DeviceRegistry
from app.storage.production_pipeline_config import ProductionPipelineConfigStore
from app.storage.production_secret_store import ProductionSecretStore
from app.storage.routing_config import RoutingConfigStore
from app.storage.run_repository import SqliteRunRepository
from app.storage.server_settings_store import ServerSettingsStore
from app.telemetry import MetricsRegistry, ProductionUsageStore, SystemMonitor


@dataclass(frozen=True)
class Components:
    base_settings: Settings
    settings: Settings
    profiles: dict[str, PipelineProfile]
    authenticator: ClientAuthenticator
    rate_limiter: ClientRateLimiter
    generation_service: GenerationService
    generation_services: dict[str, GenerationService]
    pipeline3_service: Pipeline3Service
    artifact_store: LocalArtifactStore
    production_secret_store: ProductionSecretStore
    production_pipeline_config_store: ProductionPipelineConfigStore
    run_repository: SqliteRunRepository
    metrics: MetricsRegistry
    production_usage_store: ProductionUsageStore
    system_monitor: SystemMonitor
    operator_auth_store: OperatorAuthStore
    login_rate_limiter: LoginRateLimiter
    audit_store: AuditStore
    google_key_validator: GoogleKeyValidator
    backup_manager: BackupManager
    updater_client: UpdaterClient
    server_settings_store: ServerSettingsStore
    device_registry: DeviceRegistry
    routing_config_store: RoutingConfigStore


def build_generation_service(
    settings: Settings,
    *,
    profiles: dict[str, PipelineProfile],
    artifact_store: LocalArtifactStore,
    production_secret_store: ProductionSecretStore,
    run_repository: SqliteRunRepository,
    production_usage_store: ProductionUsageStore,
    metrics: MetricsRegistry,
    prompt_templates: dict[str, str] | None = None,
    pipeline_id: str | None = None,
    transcription_instruction: str | None = None,
    normalization_instruction: str | None = None,
    routing_config_store: RoutingConfigStore | None = None,
) -> GenerationService:
    google_api_key = lambda: (
        production_secret_store.get_google_api_key(pipeline_id) or settings.google_api_key
    )
    usage_recorder = lambda stage, body: production_usage_store.record_google_response(
        stage, body, pipeline=pipeline_id or "shared"
    )
    return GenerationService(
        input_resolver=InputResolver(
            create_transcriber(
                settings,
                google_api_key,
                usage_recorder,
                instruction=transcription_instruction,
                proxy_url=(routing_config_store.proxy_url if routing_config_store else None),
            ),
            settings.max_text_length,
        ),
        prompt_normalizer=create_prompt_normalizer(
            settings,
            google_api_key,
            usage_recorder,
            instruction=normalization_instruction,
            proxy_url=(routing_config_store.proxy_url if routing_config_store else None),
        ),
        prompt_builder=PromptBuilder(templates=prompt_templates),
        image_generator=create_image_generator(
            settings,
            google_api_key,
            usage_recorder,
            proxy_url=(routing_config_store.proxy_url if routing_config_store else None),
        ),
        palette_quantizer=PaletteQuantizer(settings.palette_cleanup_passes),
        palette_validator=PaletteValidator(0),
        artifact_store=artifact_store,
        run_repository=run_repository,
        runtime_validator=RuntimeValidator(),
        metrics=metrics,
        generation_attempts=settings.palette_generation_attempts,
    )


def build_production_services(
    settings: Settings,
    *,
    profiles: dict[str, PipelineProfile],
    config_store: ProductionPipelineConfigStore,
    artifact_store: LocalArtifactStore,
    production_secret_store: ProductionSecretStore,
    run_repository: SqliteRunRepository,
    production_usage_store: ProductionUsageStore,
    metrics: MetricsRegistry,
    routing_config_store: RoutingConfigStore,
) -> tuple[dict[str, GenerationService], Pipeline3Service]:
    services: dict[str, GenerationService] = {}
    for pipeline_id in ("pipeline-1", "pipeline-2"):
        configuration = config_store.get_pipeline(pipeline_id)
        pipeline_settings = config_store.settings_for(pipeline_id)
        services[pipeline_id] = build_generation_service(
            pipeline_settings,
            profiles=profiles,
            artifact_store=artifact_store,
            production_secret_store=production_secret_store,
            run_repository=run_repository,
            production_usage_store=production_usage_store,
            metrics=metrics,
            prompt_templates={pipeline_id: str(configuration["prompt_template"])},
            pipeline_id=pipeline_id,
            transcription_instruction=str(configuration["transcription_instruction"]),
            normalization_instruction=str(configuration["normalization_instruction"]),
            routing_config_store=routing_config_store,
        )

    pipeline_id = "pipeline-3"
    configuration = config_store.get_pipeline(pipeline_id)
    pipeline_settings = config_store.settings_for(pipeline_id)
    google_api_key = lambda: (
        production_secret_store.get_google_api_key(pipeline_id)
        or pipeline_settings.google_api_key
    )
    usage_recorder = lambda stage, body: production_usage_store.record_google_response(
        stage, body, pipeline=pipeline_id
    )
    transcriber = create_transcriber(
        pipeline_settings,
        google_api_key,
        usage_recorder,
        instruction=str(configuration["transcription_instruction"]),
        proxy_url=routing_config_store.proxy_url,
    )
    normalizer = create_prompt_normalizer(
        pipeline_settings,
        google_api_key,
        usage_recorder,
        instruction=str(configuration["normalization_instruction"]),
        output_language="russian",
        proxy_url=routing_config_store.proxy_url,
    )
    if pipeline_settings.normalization_provider == "mock":
        translator = MockPromptTranslator()
    else:
        translator = GooglePromptTranslator(
            base_url=pipeline_settings.google_api_base_url,
            api_key=google_api_key,
            model=str(configuration["google_translation_model"]),
            timeout_seconds=pipeline_settings.google_timeout_seconds,
            instruction=str(configuration["translation_instruction"]),
            usage_recorder=usage_recorder,
            proxy_url=routing_config_store.proxy_url,
        )
    if pipeline_settings.image_provider == "mock":
        variant_generator = MockImageVariantGenerator()
    else:
        variant_generator = GoogleImageVariantGenerator(
            base_url=pipeline_settings.google_api_base_url,
            api_key=google_api_key,
            model=pipeline_settings.google_image_model,
            timeout_seconds=pipeline_settings.google_timeout_seconds,
            system_instruction=str(configuration["image_system_instruction"]),
            usage_recorder=usage_recorder,
            proxy_url=routing_config_store.proxy_url,
        )
    pipeline3_service = Pipeline3Service(
        input_resolver=InputResolver(transcriber, settings.max_text_length),
        prompt_normalizer=normalizer,
        prompt_translator=translator,
        image_generator=variant_generator,
        artifact_store=artifact_store,
        run_repository=run_repository,
        metrics=metrics,
    )
    return services, pipeline3_service


def apply_production_pipeline_configuration(application) -> dict[str, object]:
    services, pipeline3_service = build_production_services(
        application.state.moonli_settings,
        profiles=application.state.pipeline_profiles,
        config_store=application.state.production_pipeline_config_store,
        artifact_store=application.state.artifact_store,
        production_secret_store=application.state.production_secret_store,
        run_repository=application.state.run_repository,
        production_usage_store=application.state.production_usage_store,
        metrics=application.state.metrics,
        routing_config_store=application.state.routing_config_store,
    )
    application.state.generation_services = services
    application.state.pipeline3_service = pipeline3_service
    return application.state.production_pipeline_config_store.get()


def apply_runtime_configuration(application) -> dict[str, object]:
    stored = application.state.server_settings_store.get()
    return stored


def build_components(settings: Settings) -> Components:
    settings.validate()
    base_settings = settings
    server_settings_store = ServerSettingsStore(
        settings.data_dir / "server-settings.json", settings
    )
    profiles = load_profiles(settings.config_dir)
    if set(profiles) != {"pipeline-1", "pipeline-2"}:
        raise ValueError("Exactly pipeline-1 and pipeline-2 profiles must be configured")
    artifact_store = LocalArtifactStore(settings.data_dir / "artifacts")
    production_secret_store = ProductionSecretStore(settings.secrets_dir)
    routing_config_store = RoutingConfigStore(settings.secrets_dir)
    run_repository = SqliteRunRepository(settings.data_dir / "runs.sqlite3")
    production_usage_store = ProductionUsageStore(
        settings.data_dir / "production-usage.sqlite3",
        settings.usage_retention_days,
        settings.usage_max_rows,
    )
    device_registry = DeviceRegistry(settings.data_dir / "devices.sqlite3")
    audit_store = AuditStore(
        settings.data_dir / "audit.sqlite3",
        settings.audit_retention_days,
        settings.audit_max_events,
        settings.audit_max_bytes,
    )
    operator_auth_store = OperatorAuthStore(
        settings.data_dir / "operator-auth.sqlite3",
        settings.operator_access_key,
        settings.operator_session_ttl_minutes,
    )
    system_monitor = SystemMonitor(settings.data_dir)
    metrics = MetricsRegistry()
    authenticator = ClientAuthenticator(settings.api_keys)
    rate_limiter = ClientRateLimiter(
        requests_per_minute=settings.requests_per_minute,
        concurrent_requests=settings.concurrent_requests_per_client,
    )
    stored_configuration = server_settings_store.get()
    production_pipeline_config_store = ProductionPipelineConfigStore(
        settings.data_dir / "production-pipelines.json",
        settings,
        stored_configuration["prompt_templates"],
    )
    fallback_settings = (
        production_pipeline_config_store.settings_for("pipeline-1")
        if settings.environment == "production"
        else settings
    )
    generation_service = build_generation_service(
        fallback_settings,
        profiles=profiles,
        artifact_store=artifact_store,
        production_secret_store=production_secret_store,
        run_repository=run_repository,
        production_usage_store=production_usage_store,
        metrics=metrics,
        prompt_templates=stored_configuration["prompt_templates"],
        pipeline_id="pipeline-1" if settings.environment == "production" else None,
        routing_config_store=routing_config_store,
    )
    generation_services, pipeline3_service = build_production_services(
        settings,
        profiles=profiles,
        config_store=production_pipeline_config_store,
        artifact_store=artifact_store,
        production_secret_store=production_secret_store,
        run_repository=run_repository,
        production_usage_store=production_usage_store,
        metrics=metrics,
        routing_config_store=routing_config_store,
    )
    backup_manager = BackupManager(
        data_dir=settings.data_dir,
        runs_database=run_repository.database_path,
        usage_database=production_usage_store.database_path,
        audit_store=audit_store,
        operator_auth_store=operator_auth_store,
        server_settings_store=server_settings_store,
        device_database=device_registry.database_path,
        max_compressed_bytes=settings.backup_max_compressed_bytes,
        max_uncompressed_bytes=settings.backup_max_uncompressed_bytes,
        app_version=settings.app_version,
    )
    updater_client = UpdaterClient(
        settings.updater_socket_path,
        settings.updater_control_token,
        settings.backend_repository_url,
        backup_manager,
    )
    return Components(
        base_settings=base_settings,
        settings=settings,
        profiles=profiles,
        authenticator=authenticator,
        rate_limiter=rate_limiter,
        generation_service=generation_service,
        generation_services=generation_services,
        pipeline3_service=pipeline3_service,
        artifact_store=artifact_store,
        production_secret_store=production_secret_store,
        production_pipeline_config_store=production_pipeline_config_store,
        run_repository=run_repository,
        metrics=metrics,
        production_usage_store=production_usage_store,
        system_monitor=system_monitor,
        operator_auth_store=operator_auth_store,
        login_rate_limiter=LoginRateLimiter(),
        audit_store=audit_store,
        google_key_validator=GoogleKeyValidator(
            settings.google_api_base_url,
            settings.google_timeout_seconds,
            routing_config_store.proxy_url,
        ),
        backup_manager=backup_manager,
        updater_client=updater_client,
        server_settings_store=server_settings_store,
        device_registry=device_registry,
        routing_config_store=routing_config_store,
    )
