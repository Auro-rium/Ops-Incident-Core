from __future__ import annotations

from incidentops.config.settings import Settings

_DEFAULT_JWT_SECRETS = {
    "",
    "incidentops-dev-secret",
    "incidentops-local-jwt-secret-change-me",
    "change-me",
    "changeme",
}


def validate_startup_settings(settings: Settings) -> None:
    errors = production_settings_errors(settings)
    if errors:
        formatted = "; ".join(errors)
        raise RuntimeError(f"Unsafe production configuration: {formatted}")


def production_settings_errors(settings: Settings) -> list[str]:
    if not settings.is_production_like:
        return []

    errors: list[str] = []
    jwt_secret = settings.jwt_secret.strip()
    if jwt_secret in _DEFAULT_JWT_SECRETS or len(jwt_secret) < 32:
        errors.append("JWT_SECRET must be set to a strong non-default value")
    if settings.db_create_all:
        errors.append("DB_CREATE_ALL must be false in staging/production")
    if settings.demo_mode_public:
        errors.append("DEMO_MODE_PUBLIC must be false in staging/production")
    if settings.allow_demo_project_bypass:
        errors.append("ALLOW_DEMO_PROJECT_BYPASS must be false in staging/production")
    if settings.allow_local_seed_admin:
        errors.append("ALLOW_LOCAL_SEED_ADMIN must be false in staging/production")
    if settings.rate_limit_enabled and settings.rate_limit_backend == "memory":
        errors.append("RATE_LIMIT_BACKEND=memory is not allowed in staging/production")
    if settings.worker_mode != "queue":
        errors.append("WORKER_MODE=queue is required in staging/production")
    if settings.job_queue_backend != "redis":
        errors.append("JOB_QUEUE_BACKEND=redis is required in staging/production")
    if settings.job_queue_backend == "redis" and not settings.redis_url.strip():
        errors.append("REDIS_URL is required when JOB_QUEUE_BACKEND=redis")
    if settings.metrics_backend == "memory":
        errors.append("METRICS_BACKEND=memory is not allowed as the only metrics backend in staging/production")
    if settings.local_ingest_enabled:
        errors.append("LOCAL_INGEST_ENABLED=true is not allowed in staging/production")
    if settings.cors_origins_list == ["*"] and not settings.allow_wildcard_cors:
        errors.append("CORS wildcard is disabled but CORS_ALLOW_ORIGINS=*")
    if settings.cors_origins_list == ["*"] and settings.allow_wildcard_cors:
        errors.append("CORS wildcard is not allowed in staging/production")
    return errors
