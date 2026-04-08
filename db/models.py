from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class VMStatus(str, Enum):
    pending = "pending"
    provisioning = "provisioning"
    downloading = "downloading"
    running = "running"
    evicted = "evicted"
    terminated = "terminated"


class Base(DeclarativeBase):
    pass


class LLMModel(Base):
    __tablename__ = "llm_models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(1000))
    size_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    # Ollama model tag, e.g. "llama3:8b" or "mistral:7b"
    model_identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    vm_size: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    instances: Mapped[list[VMInstance]] = relationship(
        "VMInstance", back_populates="model", cascade="all, delete-orphan"
    )


class VMInstance(Base):
    __tablename__ = "vm_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("llm_models.id"), nullable=False
    )
    vm_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    resource_group: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(100))
    ip_address: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[VMStatus] = mapped_column(
        SAEnum(VMStatus), nullable=False, default=VMStatus.pending
    )
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    model: Mapped[LLMModel] = relationship("LLMModel", back_populates="instances")
