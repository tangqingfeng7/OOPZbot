"""后台成员页「域」参数的前端契约测试
"""

from __future__ import annotations

import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "src/web/assets/admin/pages/members_script.js"

# 脚本是注入到模板里的片段（顶层是 var 声明），包一层函数即可独立求值。
HARNESS = """
async () => {
  const results = [];
  const run = async (area, map, picked) => {
    let currentArea = area;
    let memberAreaMap = map;
    let askedWith = null;
    const AdminShell = {
      pickArea: (areas) => { askedWith = areas.map((a) => a.areaId); return Promise.resolve(picked); },
    };
    %(fns)s
    return {
      listArea: getArea(),
      opArea: await getMemberArea("u1"),
      opAreaUnknown: await getMemberArea("nope"),
      primary: getMemberAreaPrimary("u1"),
      askedWith,
    };
  };
  results.push(["single", await run("A", {}, "")]);
  results.push(["all", await run("__all__", { u1: [{ areaId: "B", areaName: "B域" }] }, "")]);
  const multi = { u1: [{ areaId: "B", areaName: "B域" }, { areaId: "C", areaName: "C域" }] };
  results.push(["multi", await run("__all__", multi, "C")]);
  results.push(["multiCancelled", await run("__all__", multi, "")]);
  return results;
}
"""


def _extract(name: str) -> str:
    """从脚本中截取某个顶层函数的源码。"""
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    if src[:start].rstrip().endswith("async"):
        start = src.rindex("async", 0, start)
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1


class MembersAreaScopeTest(unittest.TestCase):
    def test_area_scope_contract(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover
            self.skipTest("playwright 未安装")

        fns = "\n".join(
            _extract(name) for name in ("getArea", "getMemberArea", "getMemberAreaPrimary")
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                results = dict(page.evaluate(HARNESS % {"fns": fns}))
            finally:
                browser.close()

        single = results["single"]
        self.assertEqual(single["listArea"], "A")
        self.assertEqual(single["opArea"], "A", "选中单个域时，管理操作就用该域")

        every = results["all"]
        self.assertEqual(
            every["listArea"],
            "__all__",
            "选中「全部域」时列表必须请求 __all__，返回空串会让后端回落到默认域",
        )
        self.assertEqual(every["opArea"], "B", "管理操作要落到成员真实所在的域")
        self.assertEqual(every["opAreaUnknown"], "", "域未知时返回空串，由后端拒绝而非误伤其它域")
        self.assertIsNone(every["askedWith"], "只属于一个域时不该打扰操作者")

        multi = results["multi"]
        self.assertEqual(
            multi["askedWith"], ["B", "C"], "同属多个域时必须让操作者选，不能静默挑一个"
        )
        self.assertEqual(multi["opArea"], "C", "要用操作者选中的域")
        self.assertEqual(multi["primary"], "B", "只读展示取权限最高的主域，不弹窗")

        self.assertEqual(
            results["multiCancelled"]["opArea"], "", "取消选择时返回空串，调用方据此中止操作"
        )


if __name__ == "__main__":
    unittest.main()
