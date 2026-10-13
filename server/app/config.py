from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str = "us-east-1"
    dynamodb_table: str = "homie-dev"
    dynamodb_endpoint_url: str | None = None
    cors_origins: list[str] = ["http://localhost:3000"]
    jwt_secret: str = ""
    allow_mock_auth: bool = False
    google_maps_api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""


settings = Settings()
