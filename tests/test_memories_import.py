import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "migrate"))

from claude_memories import tidy  # noqa: E402


def test_tidy_memory_file():
    src = ("---\nname: x\ndescription: 她想做的产品，核心是双向选择\nsources: [chat]\n"
           "aliases: [双生, 产品]\n---\n\n## 关于她\n\n- [stated] 用 `hold` 写\n"
           "- [stated] 见 [[dual-sheng]]，**别忘**\n> 原话\n")
    name, body, aliases = tidy(src)
    assert name == "她想做的产品"
    assert aliases == ["双生", "产品"]
    assert body == ("她想做的产品，核心是双向选择\n\n【关于她】\n\n"
                    "· 用 hold 写\n· 见 dual-sheng，别忘\n　　原话")


def test_tidy_summary_headings():
    name, body, _ = tidy("**Work context**\n\nShe works.\n\n*Recent months*\n\nBusy.")
    assert name == ""
    assert body == "【工作】\n\nShe works.\n\n· 最近几个月 ·\n\nBusy."
