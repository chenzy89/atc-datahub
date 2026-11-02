# -*- coding: utf-8 -*-
"""从《航空公司代码》表生成 config/airline_domestic.json

用法：
    # A. 直接读 xls（需要 xlrd：/usr/bin/python3 -m pip install --user xlrd）
    python3 scripts/build_airline_region.py config/航空公司代码.xls

    # B. 没有 xlrd 时，把 xls 另存为 CSV 再跑
    python3 scripts/build_airline_region.py config/航空公司代码.csv

规则：「是否国内」列 Checked → 境内，Unchecked → 境外
输出：config/airline_domestic.json（domestic / foreign / names）
"""

from __future__ import annotations

import csv
import datetime
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "config" / "airline_domestic.json"
DEFAULT_SRC = Path(__file__).resolve().parent.parent / "config" / "航空公司代码.xls"


def read_rows(path: Path) -> list[list[str]]:
    """返回 [表头, 数据行...]，每行 6 列"""
    suf = path.suffix.lower()
    if suf in (".csv", ".tsv", ".txt"):
        delim = "\t" if suf in (".tsv", ".txt") else ","
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [row for row in csv.reader(f, delimiter=delim)]
    if suf in (".xls", ".xlsx"):
        try:
            import xlrd  # type: ignore
        except ImportError:
            sys.exit(
                "缺少 xlrd，无法直读 %s。\n"
                "  方案1: /usr/bin/python3 -m pip install --user xlrd\n"
                "  方案2: 用 WPS/Excel 另存为 CSV 后传入该 CSV 路径" % path.name
            )
        book = xlrd.open_workbook(str(path))
        sh = book.sheet_by_index(0)
        return [[str(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(sh.nrows)]
    sys.exit("不支持的文件类型: %s（支持 .xls/.xlsx/.csv/.tsv）" % path.name)


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.exists():
        sys.exit("源文件不存在: %s" % src)

    rows = read_rows(src)
    if not rows:
        sys.exit("源文件为空")

    header = rows[0]
    try:
        i_code = next(i for i, h in enumerate(header) if "三字码" in str(h))
        i_flag = next(i for i, h in enumerate(header) if "国内" in str(h))
    except StopIteration:
        sys.exit("表头未找到「三字码」或「是否国内」列，实际表头: %s" % header)
    i_name = next((i for i, h in enumerate(header) if "中文名称" in str(h)), None)

    domestic, foreign, names = [], [], {}
    for row in rows[1:]:
        if len(row) <= max(i_code, i_flag):
            continue
        code = str(row[i_code]).strip().upper()
        flag = str(row[i_flag]).strip()
        if not code or code in names:
            continue
        names[code] = str(row[i_name]).strip() if i_name is not None and len(row) > i_name else ""
        (domestic if flag == "Checked" else foreign).append(code)

    out = {
        "source": "config/%s" % src.name,
        "generated": datetime.date.today().isoformat(),
        "note": "是否国内列：Checked=境内，Unchecked=境外；未收录的三字码按中国注册号规则(^B+数字)判境内，其余判境外",
        "domestic": sorted(domestic),
        "foreign": sorted(foreign),
        "names": names,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写入 %s" % OUT)
    print("  境内 %d 个 / 境外 %d 个 / 共 %d 个三字码" % (len(domestic), len(foreign), len(names)))


if __name__ == "__main__":
    main()
