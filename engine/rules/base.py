from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from engine.models import Dataset, ErrorKind, Issue, RuleBinding, Tier


class NotFixable(Exception):
    """결정론적으로 수정할 수 없는 경우 fix()에서 발생."""


@dataclass
class NormResult:
    """정규화 함수의 표준 반환형."""
    value: str | None
    certain: bool
    reason: str


@dataclass
class RuleContext:
    """규칙 실행 시 공유 컨텍스트."""
    issue_counter: list[int] = field(default_factory=lambda: [0])
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def next_issue_id(self) -> str:
        self.issue_counter[0] += 1
        return f"E-{self.issue_counter[0]:04d}"


class Rule(ABC):
    rule_id: ClassVar[str]
    kind: ClassVar[ErrorKind] = "rule"
    default_tier: ClassVar[Tier]
    scope: ClassVar[Literal["cell", "row", "table", "cross_sheet"]]

    @abstractmethod
    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        ...

    def fix(self, value: str | None, issue: Issue) -> str | None:
        raise NotFixable


REGISTRY: dict[str, type[Rule]] = {}


def register(cls: type[Rule]) -> type[Rule]:
    REGISTRY[cls.rule_id] = cls
    return cls
