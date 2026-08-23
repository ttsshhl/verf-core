import enum
import secrets
import uuid
from datetime import datetime, timezone

def utcnow():
    return datetime.now(timezone.utc)

from sqlalchemy import Column, String, DateTime, ForeignKey, Enum, Text, Integer
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


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, default=gen_id)
    slug = Column(String, unique=True, nullable=False, index=True)
    repo_url = Column(String, nullable=False)
    branch = Column(String, default="main")
    kind = Column(Enum(ProjectKind), default=ProjectKind.backend)
    webhook_secret = Column(String, default=gen_secret)  # per-project secret used to verify GitHub payloads
    env_json = Column(Text, default="{}")  # serialized dict of env vars injected into the container
    created_at = Column(DateTime, default=utcnow)

    deployments = relationship("Deployment", back_populates="project", order_by="Deployment.created_at.desc()")


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
