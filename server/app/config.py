from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_origins: list[str] = ["http://localhost:3000"]
    google_maps_api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""


settings = Settings()
