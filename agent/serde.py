"""pickle 기반 LangGraph 체크포인트 직렬화."""
from __future__ import annotations

import pickle
from typing import Any


class PickleSerde:
    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        return "pickle", pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_, payload = data
        if type_ == "pickle":
            return pickle.loads(payload)
        raise ValueError(f"알 수 없는 직렬화 타입: {type_}")
