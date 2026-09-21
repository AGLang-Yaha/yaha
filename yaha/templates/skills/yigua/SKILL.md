---
name: yigua
description: 使用金钱法进行易经起卦 (YiGua using the Coin Method).
resources:
  - scripts/yigua.py
---

# Instructions
当用户想要起卦、占卜或询问运势时，你可以使用这个技能。

1. **执行脚本**: 使用 `bash_tool` 运行位于该技能目录下的 `scripts/yigua.py` 脚本。
   - 命令格式: `python3 <skill_location>/scripts/yigua.py`
2. **分析结果**: 脚本会输出六个爻的结果（从初爻到上爻），以及“本卦”和“变卦”的图形表示。
3. **解读**: 你需要根据起出的卦象，结合《易经》的知识为用户提供解读。
   - 注意区分本卦（当前状态）和变卦（未来趋势）。
   - 如果没有变爻，则只需解读本卦。

# Example
<tool_code>
{
  "tool": "bash_tool",
  "args": {
    "cmdline": "python3 .yaha/skills/yigua/scripts/yigua.py"
  }
}
</tool_code>
