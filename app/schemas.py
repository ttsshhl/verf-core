from pydantic import BaseModel, Field, ConfigDict, model_validator


class ProjectCreate(BaseModel):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", description="используется как поддомен")
    repo_url: str | None = None
    repo_full_name: str | None = None  # "owner/repo" — set when picked from the GitHub repo list instead of typed
    branch: str = "main"
    kind: str = "backend"  # site | bot | backend
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_one_repo_source(self):
        if not self.repo_url and not self.repo_full_name:
            raise ValueError("Укажи repo_url или repo_full_name")
        return self


class ProjectOut(BaseModel):
    id: str
    slug: str
    repo_url: str
    branch: str
    kind: str
    webhook_secret: str
    webhook_auto_configured: bool
    url: str

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    email: str
    password: str = Field(..., min_length=8)


class UserLogin(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    plan: str
    github_connected: bool
    github_username: str | None = None

    model_config = ConfigDict(from_attributes=True)


class GithubRepoOut(BaseModel):
    name: str
    full_name: str
    default_branch: str
    private: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SubscribeRequest(BaseModel):
    plan: str  # "pro" | "business"
    provider: str = "yookassa"  # "yookassa" | "cryptomus"


class SubscriptionOut(BaseModel):
    id: str
    plan: str
    status: str
    amount_rub: int
    confirmation_url: str | None = None

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
