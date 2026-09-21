# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

import random
import sys

def toss_coins():
    """Simulates tossing 3 coins."""
    # 0 for Tails (Yin, 2 points), 1 for Heads (Yang, 3 points)
    tosses = [random.randint(0, 1) for _ in range(3)]
    points = sum([3 if t == 1 else 2 for t in tosses])
    return points

def get_line_type(points):
    if points == 6: return "老陰 (6) --x--"
    if points == 7: return "少陽 (7) ----- "
    if points == 8: return "少陰 (8) -- --"
    if points == 9: return "老陽 (9) --o--"
    return "Error"

def jigua():
    print("--- 正在金钱法起卦 (Jigua using Coin Method) ---")
    lines = []
    for i in range(6):
        points = toss_coins()
        lines.append(points)
        print(f"第 {i+1} 爻: {get_line_type(points)}")
    
    # Bottom to top representation
    print("\n本卦 (Original Hexagram):")
    for p in reversed(lines):
        if p in [7, 9]: print("-----")
        else: print("-- --")
        
    # Check for changing lines
    has_change = any(p in [6, 9] for p in lines)
    if has_change:
        print("\n变卦 (Changed Hexagram):")
        for p in reversed(lines):
            if p == 6: # Old Yin becomes Yang
                print("-----")
            elif p == 9: # Old Yang becomes Yin
                print("-- --")
            elif p == 7: # Yang stays Yang
                print("-----")
            else: # Yin stays Yin
                print("-- --")
    else:
        print("\n无变爻 (No changing lines).")

if __name__ == "__main__":
    jigua()
