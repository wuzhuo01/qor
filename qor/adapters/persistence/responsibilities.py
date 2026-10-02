"""Responsibility repository.

Same in-memory entity store as the other aggregates. The one-active rule is
enforced here so a second ACTIVE row cannot be inserted for the same source.
"""

from __future__ import annotations

from uuid import UUID

from qor.adapters.persistence.inmemory import InMemoryLogStore, InMemoryRepository
from qor.domain.enums import ResponsibilitySource
from qor.domain.responsibility import Responsibility
from qor.kernel.errors import QORError

__all__ = ["ResponsibilityRepository"]


class ResponsibilityRepository(InMemoryRepository[Responsibility]):
    def __init__(self, log: InMemoryLogStore) -> None:
        super().__init__("Responsibility", log)

    def list_by_source(
        self, source_type: ResponsibilitySource, source_id: UUID
    ) -> tuple[Responsibility, ...]:
        return tuple(
            item
            for item in self.all()
            if item.source_type == source_type and item.source_id == source_id
        )

    def find_active_by_source(
        self, source_type: ResponsibilitySource, source_id: UUID
    ) -> Responsibility | None:
        matches = [
            item for item in self.list_by_source(source_type, source_id) if item.status == "ACTIVE"
        ]
        if len(matches) > 1:
            raise QORError(
                f"source {source_type.value}:{source_id} has more than one ACTIVE responsibility"
            )
        return matches[0] if matches else None

    def insert(self, entity: Responsibility) -> Responsibility:
        if entity.status == "ACTIVE":
            current = self.find_active_by_source(entity.source_type, entity.source_id)
            if current is not None:
                raise QORError(
                    f"source {entity.source_type.value}:{entity.source_id} "
                    f"already has ACTIVE responsibility {current.ref}"
                )
        return super().insert(entity)

    def update(self, entity: Responsibility) -> Responsibility:
        return self.save(entity)
