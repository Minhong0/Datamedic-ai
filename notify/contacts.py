"""기관 주소록 — 보완·확인 요청 메일의 수신자를 기관명으로 찾는다."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd

from config import settings

logger = logging.getLogger(__name__)

ORG_COLUMN = "기관명"
MAIL_COLUMN = "담당자메일"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def contacts_path() -> Path:
    return settings.standard_dir / "org_contacts.csv"


def load_contacts(path: Path | None = None) -> dict[str, str]:
    """{기관명: 메일}. 파일이 없거나 열이 없으면 빈 사전, 형식이 맞지 않는 주소는 건너뛴다."""
    p = path or contacts_path()
    if not p.exists():
        return {}
    df = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            df = pd.read_csv(p, dtype=str, encoding=encoding).fillna("")
            break
        except UnicodeDecodeError:
            continue
        except (OSError, ValueError) as e:
            logger.warning("주소록을 읽지 못했습니다: %s", e)
            return {}
    if df is None:
        logger.warning("주소록의 문자 인코딩을 알 수 없습니다 (UTF-8 또는 CP949 로 저장해 주세요): %s", p)
        return {}
    if ORG_COLUMN not in df.columns or MAIL_COLUMN not in df.columns:
        logger.warning("주소록에 '%s', '%s' 열이 필요합니다: %s", ORG_COLUMN, MAIL_COLUMN, p)
        return {}
    out: dict[str, str] = {}
    for org, mail in zip(df[ORG_COLUMN].str.strip(), df[MAIL_COLUMN].str.strip()):
        if org and _EMAIL_RE.match(mail):
            out[org] = mail
    return out


def org_of_file(file: str, dataset=None) -> str | None:
    """파일이 어느 기관 것인지 — 데이터의 `기관명` 열 최빈값을 우선하고, 없으면 파일 이름의 앞부분(`창원시_사업실적.xlsx` → 창원시)."""
    if dataset is not None:
        values: list[str] = []
        for (f, _sheet), df in dataset.tables.items():
            if f == file and ORG_COLUMN in df.columns:
                values += [str(v).strip() for v in df[ORG_COLUMN] if str(v).strip() and str(v).lower() != "nan"]
        if values:
            return max(set(values), key=values.count)
    stem = Path(file).stem
    return stem.split("_")[0].strip() or None


def resolve_recipient(file: str, dataset=None, contacts: dict[str, str] | None = None) -> str | None:
    """파일에 해당하는 기관의 담당자 메일. 주소록에 없으면 None."""
    book = contacts if contacts is not None else load_contacts()
    org = org_of_file(file, dataset)
    return book.get(org) if org else None
