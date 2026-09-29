from pathlib import Path

import pytest

from engine.parser import load_files
from engine.models import CellRef

FIXTURES = Path(__file__).parent / "fixtures"


def test_xlsx_loads_as_str():
    ds = load_files([FIXTURES / "sample.xlsx"])
    df = ds.tables[("sample.xlsx", "실적")]
    assert isinstance(df["지원금액"].iloc[0], str)
    assert df["지원금액"].iloc[0] == "1000000"


def test_xlsx_row_numbers():
    ds = load_files([FIXTURES / "sample.xlsx"])
    df = ds.tables[("sample.xlsx", "실적")]
    # 헤더=1, 첫 데이터=2
    assert list(df["_row"]) == [2, 3, 4]


def test_serial_date_converted():
    import pandas as pd
    epoch = pd.Timestamp("1899-12-30")
    ds = load_files([FIXTURES / "serial_date.xlsx"])
    df = ds.tables[("serial_date.xlsx", "실적")]
    # 45930, 45931 → 각각 epoch+45930일, epoch+45931일
    expected_0 = (epoch + pd.Timedelta(days=45930)).strftime("%Y-%m-%d")
    expected_1 = (epoch + pd.Timedelta(days=45931)).strftime("%Y-%m-%d")
    assert df["지원일자"].iloc[0] == expected_0
    assert df["지원일자"].iloc[1] == expected_1


def test_json_loads():
    ds = load_files([FIXTURES / "sample.json"])
    df = ds.tables[("sample.json", "root")]
    assert len(df) == 3
    assert df["org"].iloc[0] == "창원시"


def test_json_row_numbers():
    ds = load_files([FIXTURES / "sample.json"])
    df = ds.tables[("sample.json", "root")]
    assert list(df["_row"]) == [2, 3, 4]


def test_multisheet_xlsx():
    ds = load_files([FIXTURES / "multisheet.xlsx"])
    assert ("multisheet.xlsx", "실적") in ds.tables
    assert ("multisheet.xlsx", "요약") in ds.tables


def test_cell_access():
    ds = load_files([FIXTURES / "sample.xlsx"])
    ref = CellRef(file="sample.xlsx", sheet="실적", row=3, column="기관명")
    assert ds.cell(ref) == "진주시"


def test_cell_missing_row():
    ds = load_files([FIXTURES / "sample.xlsx"])
    ref = CellRef(file="sample.xlsx", sheet="실적", row=99, column="기관명")
    assert ds.cell(ref) is None


def test_multiple_files():
    ds = load_files([
        FIXTURES / "sample.xlsx",
        FIXTURES / "sample.json",
    ])
    assert ("sample.xlsx", "실적") in ds.tables
    assert ("sample.json", "root") in ds.tables


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_files(["nonexistent.xlsx"])


def test_unsupported_extension():
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(b"a,b\n1,2\n")
        tmp = f.name
    try:
        with pytest.raises(ValueError, match="지원하지 않는 형식"):
            load_files([tmp])
    finally:
        os.unlink(tmp)
