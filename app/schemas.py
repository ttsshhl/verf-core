from pydantic import BaseModel, Field, ConfigDict


class ProjectCreate(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", description="используется как поддомен")
    repo_url: str
    branch: str = "main"
    kind: str = "backend"  # site | bot | backend
    env: dict[str, str] = Field(default_factory=dict)


class ProjectOut(BaseModel):
    id: str
    slug: str
    repo_url: str
    branch: str
    kind: str
    webhook_secret: str
    url: str

    model_config = ConfigDict(from_attributes=True)


class DeploymentOut(BaseModel):
    id: str
    project_id: str
    commit_sha: str | None
    status: str
    log: str
    port: int | None
    created_at: str
    finished_at: str | None

    model_config = ConfigDict(from_attributes=True)
