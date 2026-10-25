"""One immutable complete Core root for an accepted Modem Live generation."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

from arch4.architecture_contracts.runtime_generation import (
    MatrixGenerationIdentity,
)
from arch4.controller_frames import (
    RadioTransitionFrame,
    radio_transition_frame_to_mapping,
)
from arch4.core_contracts.accepted_generation_commit import (
    AcceptedGenerationCommit,
)
from arch4.core_contracts.accepted_runtime_view import (
    CoreAcceptedRuntimeView,
)
from arch4.core_contracts.accepted_current_facts import (
    CoreAcceptedCurrentFacts,
)
from arch4.core_contracts.observations import CoreObservationFrame
from arch4.runtime_registers import SignalProcessingRegisterTransaction


CORE_ACCEPTED_GENERATION_ROOT_SCHEMA = "CoreAcceptedGenerationRoot.v1"
CORE_ACCEPTED_GENERATION_ROOT_OWNER = "54guc.core.accepted_generation_root"


@dataclass(frozen=True, slots=True)
class CoreAcceptedGenerationRoot:
    matrix_identity: MatrixGenerationIdentity
    commit: AcceptedGenerationCommit
    register_transaction: SignalProcessingRegisterTransaction
    observation_frame: CoreObservationFrame
    runtime_view: CoreAcceptedRuntimeView
    current_facts: CoreAcceptedCurrentFacts
    radio_transition: RadioTransitionFrame
    prepared_at: float
    root_fingerprint: str
    schema: str = CORE_ACCEPTED_GENERATION_ROOT_SCHEMA
    owner: str = CORE_ACCEPTED_GENERATION_ROOT_OWNER

    def __post_init__(self) -> None:
        if (
            self.schema != CORE_ACCEPTED_GENERATION_ROOT_SCHEMA
            or self.owner != CORE_ACCEPTED_GENERATION_ROOT_OWNER
        ):
            raise ValueError("core_accepted_root_owner_schema_invalid")
        if not isinstance(self.matrix_identity, MatrixGenerationIdentity):
            raise TypeError("core_accepted_root_matrix_identity_required")
        if not isinstance(self.commit, AcceptedGenerationCommit):
            raise TypeError("core_accepted_root_commit_required")
        if self.commit.matrix_identity != self.matrix_identity:
            raise ValueError("core_accepted_root_commit_identity_mismatch")
        if not isinstance(
            self.register_transaction,
            SignalProcessingRegisterTransaction,
        ):
            raise TypeError("core_accepted_root_register_transaction_required")
        if (
            self.register_transaction.matrix_identity
            != self.matrix_identity
            or self.register_transaction.commit_id != self.commit.commit_id
        ):
            raise ValueError("core_accepted_root_register_identity_mismatch")
        if not isinstance(self.observation_frame, CoreObservationFrame):
            raise TypeError("core_accepted_root_observation_frame_required")
        if (
            self.observation_frame.matrix_identity != self.matrix_identity
            or self.observation_frame.accepted_commit_id
            != self.commit.commit_id
        ):
            raise ValueError("core_accepted_root_observation_identity_mismatch")
        if not isinstance(self.runtime_view, CoreAcceptedRuntimeView):
            raise TypeError("core_accepted_root_runtime_view_required")
        if (
            self.runtime_view.matrix_identity != self.matrix_identity
            or self.runtime_view.accepted_commit_id
            != self.commit.commit_id
        ):
            raise ValueError("core_accepted_root_runtime_view_identity_mismatch")
        if not isinstance(self.current_facts, CoreAcceptedCurrentFacts):
            raise TypeError("core_accepted_root_current_facts_required")
        if self.current_facts.matrix_identity != self.matrix_identity:
            raise ValueError(
                "core_accepted_root_current_facts_identity_mismatch"
            )
        if not isinstance(self.radio_transition, RadioTransitionFrame):
            raise TypeError("core_accepted_root_radio_transition_required")
        if (
            self.radio_transition.matrix_identity
            != self.matrix_identity
        ):
            raise ValueError("core_accepted_root_transition_identity_mismatch")
        if type(self.prepared_at) not in {int, float}:
            raise TypeError("core_accepted_root_prepared_at_invalid")
        prepared_at = float(self.prepared_at)
        if (
            not math.isfinite(prepared_at)
            or prepared_at < self.commit.committed_at
        ):
            raise ValueError("core_accepted_root_prepared_at_invalid")
        material = self._fingerprint_material()
        expected = hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if type(self.root_fingerprint) is not str:
            raise TypeError("core_accepted_root_fingerprint_invalid")
        if self.root_fingerprint != expected:
            raise ValueError("core_accepted_root_fingerprint_mismatch")
        object.__setattr__(
            self,
            "register_transaction",
            copy.deepcopy(self.register_transaction),
        )
        object.__setattr__(self, "prepared_at", prepared_at)

    @classmethod
    def prepare(
        cls,
        *,
        commit: AcceptedGenerationCommit,
        register_transaction: SignalProcessingRegisterTransaction,
        observation_frame: CoreObservationFrame,
        runtime_view: CoreAcceptedRuntimeView,
        current_facts: CoreAcceptedCurrentFacts,
        radio_transition: RadioTransitionFrame,
        prepared_at: float,
    ) -> "CoreAcceptedGenerationRoot":
        provisional = object.__new__(cls)
        object.__setattr__(provisional, "matrix_identity", commit.matrix_identity)
        object.__setattr__(provisional, "commit", commit)
        object.__setattr__(
            provisional,
            "register_transaction",
            register_transaction,
        )
        object.__setattr__(provisional, "observation_frame", observation_frame)
        object.__setattr__(provisional, "runtime_view", runtime_view)
        object.__setattr__(provisional, "current_facts", current_facts)
        object.__setattr__(provisional, "radio_transition", radio_transition)
        object.__setattr__(provisional, "prepared_at", prepared_at)
        object.__setattr__(provisional, "root_fingerprint", "")
        object.__setattr__(
            provisional,
            "schema",
            CORE_ACCEPTED_GENERATION_ROOT_SCHEMA,
        )
        object.__setattr__(
            provisional,
            "owner",
            CORE_ACCEPTED_GENERATION_ROOT_OWNER,
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                provisional._fingerprint_material(),
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            matrix_identity=commit.matrix_identity,
            commit=commit,
            register_transaction=register_transaction,
            observation_frame=observation_frame,
            runtime_view=runtime_view,
            current_facts=current_facts,
            radio_transition=radio_transition,
            prepared_at=prepared_at,
            root_fingerprint=fingerprint,
        )

    def _fingerprint_material(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner": self.owner,
            "matrix_identity": self.matrix_identity.as_dict(),
            "commit": self.commit.to_payload(),
            "register_transaction": {
                "commit_id": self.register_transaction.commit_id,
                "matrix_identity": (
                    self.register_transaction.matrix_identity.as_dict()
                ),
                "writes": [
                    {
                        **asdict(write),
                        "value": {
                            "sha256": write.value.sha256,
                            "size_bytes": write.value.size_bytes,
                        },
                    }
                    for write in self.register_transaction.writes
                ],
            },
            "observation_frame": self.observation_frame.to_payload(),
            "runtime_view": self.runtime_view.to_payload(),
            "current_facts": self.current_facts.to_payload(),
            "radio_transition": radio_transition_frame_to_mapping(
                self.radio_transition
            ),
            "prepared_at": float(self.prepared_at),
        }


__all__ = (
    "CORE_ACCEPTED_GENERATION_ROOT_OWNER",
    "CORE_ACCEPTED_GENERATION_ROOT_SCHEMA",
    "CoreAcceptedGenerationRoot",
)
