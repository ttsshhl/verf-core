import enum
import secrets
import uuid
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc)

from sqlalchemy import Column, String, DateTime, ForeignKey, Enum, Text, Integer, Boolean
from sqlalchemy.orm import relationship

from app.db import Base


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


def gen_secret() -> str:
    return secrets.token_urlsafe(32)


class ProjectKind(str, enum.Enum):
    site = "site"
    bot = "bot"
    backend = "backend"


class DeployStatus(str, enum.Enum):
    pending = "pending"
    cloning = "cloning"
    building = "building"
    starting = "starting"
    running = "running"
    failed = "failed"


class SubscriptionStatus(str, enum.Enum):
    pending = "pending"   # payment created, waiting for confirmation
    active = "active"
    expired = "expired"
    canceled = "canceled"


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_id)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    plan = Column(String, default="free")  # "free" | "pro" | "business" — mirrors latest active subscription
    github_token = Column(String, nullable=True)  # OAuth access token, only set once the user connects GitHub
    github_username = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    projects = relationship("Project", back_populates="owner")
    subscriptions = relationship("Subscription", back_populates="user", order_by="Subscription.created_at.desc()")

    @property
    def github_connected(self) -> bool:
        return bool(self.github_token)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    plan = Column(String, nullable=False)  # "pro" | "business"
    status = Column(Enum(SubscriptionStatus), default=SubscriptionStatus.pending)
    provider = Column(String, nullable=False, default="yookassa")  # "yookassa" | "cryptomus"
    external_payment_id = Column(String, nullable=True, index=True)
    amount_rub = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    activated_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscriptions")


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=gen_id)
    owner_id = Column(String, ForeignKey("users.id"), nullable=True)  # nullable: admin-created projects may have no owner
    slug = Column(String, unique=True, nullable=False, index=True)
    repo_url = Column(String, nullable=True)  # null for projects deployed only via CLI/ZIP upload (no git remote)
    branch = Column(String, default="main")
    kind = Column(Enum(ProjectKind), default=ProjectKind.backend)
    webhook_secret = Column(String, default=gen_secret)  # per-project secret used to verify GitHub payloads
    webhook_auto_configured = Column(Boolean, default=False)  # True if VERF created the GitHub webhook itself via the API
    custom_domain = Column(String, nullable=True, unique=True)  # e.g. "example.com" — user's own domain, CNAME'd to {slug}.DOMAIN_SUFFIX
    env_json = Column(Text, default="{}")  # serialized dict of env vars injected into the container
    created_at = Column(DateTime, default=utcnow)

    owner = relationship("User", back_populates="projects")
    deployments = relationship(
        "Deployment", back_populates="project", order_by="Deployment.created_at.desc()",
        cascade="all, delete-orphan",
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(String, primary_key=True, default=gen_id)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    commit_sha = Column(String, nullable=True)
    status = Column(Enum(DeployStatus), default=DeployStatus.pending)
    log = Column(Text, default="")
    container_id = Column(String, nullable=True)
    image_tag = Column(String, nullable=True)
    port = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)

    project = relationship("Project", back_populates="deployments")
