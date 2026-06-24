"""Verify SL math with fees + slippage + liquidation."""

# HYPE setup
HYPE = {
    "entry": 58.90,
    "sl": 58.30,
    "tp": 60.50,
    "qty": 1.53,
    "margin": 3.0,
    "lev": 30,
}

# EDEN setup
EDEN = {
    "entry": 0.0967,
    "sl": 0.0950,
    "tp": 0.1020,
    "qty": 414,
    "margin": 2.0,
    "lev": 20,
}

TAKER_FEE = 0.0005  # 0.05% per side

def analyze(name, p):
    print(f"\n=== {name} ===")
    notional = p["entry"] * p["qty"]
    print(f"Notional: ${notional:.2f}")
    print(f"Margin: ${p['margin']}  Lev: {p['lev']}x")

    # Liquidation price (isolated, lowest tier MMR ~1.25%)
    mmr = 0.0125
    liq_long = p["entry"] * (1 - 1/p["lev"] + mmr)
    print(f"Estimated liq: ${liq_long:.5f}  (distance from entry: {(liq_long/p['entry']-1)*100:+.2f}%)")
    print(f"SL ${p['sl']} vs liq ${liq_long:.5f}: {'OK (SL fires first)' if p['sl'] > liq_long else 'DANGER liq before SL!'}")

    # Best case fill (no slippage)
    price_loss_best = (p["entry"] - p["sl"]) * p["qty"]
    fee_open = notional * TAKER_FEE
    fee_close_sl = p["sl"] * p["qty"] * TAKER_FEE
    total_loss_best = price_loss_best + fee_open + fee_close_sl
    print(f"\nBest case SL (no slippage):")
    print(f"  Price loss: ${price_loss_best:.3f}")
    print(f"  Open fee: ${fee_open:.3f}")
    print(f"  Close fee: ${fee_close_sl:.3f}")
    print(f"  TOTAL: ${total_loss_best:.3f}")

    # Worst case with slippage 0.1% entry up, 0.1% exit down
    slip_entry = p["entry"] * 1.001
    slip_sl = p["sl"] * 0.999
    price_loss_worst = (slip_entry - slip_sl) * p["qty"]
    fee_open_w = slip_entry * p["qty"] * TAKER_FEE
    fee_close_w = slip_sl * p["qty"] * TAKER_FEE
    total_loss_worst = price_loss_worst + fee_open_w + fee_close_w
    print(f"\nWorst case SL (0.1% slip each way):")
    print(f"  Entry fill: ${slip_entry:.5f}, SL fill: ${slip_sl:.5f}")
    print(f"  Price loss: ${price_loss_worst:.3f}")
    print(f"  Fees: ${fee_open_w+fee_close_w:.3f}")
    print(f"  TOTAL: ${total_loss_worst:.3f}")

    # TP math
    tp_gain_best = (p["tp"] - p["entry"]) * p["qty"]
    fee_tp = p["tp"] * p["qty"] * TAKER_FEE
    net_tp = tp_gain_best - fee_open - fee_tp
    print(f"\nTP profit (best case):")
    print(f"  Price gain: ${tp_gain_best:.3f}")
    print(f"  Fees: ${fee_open + fee_tp:.3f}")
    print(f"  NET: ${net_tp:.3f}")

    return total_loss_best, total_loss_worst, net_tp

h_best, h_worst, h_tp = analyze("HYPE LONG", HYPE)
e_best, e_worst, e_tp = analyze("EDEN LONG", EDEN)

print(f"\n=== COMBINED ===")
print(f"Best case both SL: ${h_best+e_best:.2f}")
print(f"Worst case both SL: ${h_worst+e_worst:.2f}")
print(f"Best case both TP: ${h_tp+e_tp:.2f}")

print(f"\n=== WALLET IMPACT ===")
wallet = 8.55
print(f"Start: ${wallet}")
print(f"If both SL (best): ${wallet - h_best - e_best:.2f}")
print(f"If both SL (worst): ${wallet - h_worst - e_worst:.2f}")
print(f"If both TP: ${wallet + h_tp + e_tp:.2f}")

# Tick alignment check
print(f"\n=== TICK ALIGNMENT ===")
print(f"HYPE tick 0.001. SL $58.30 → {58.30 / 0.001} (must be integer): {'OK' if (58.30 / 0.001) == round(58.30/0.001) else 'MISALIGNED'}")
print(f"HYPE TP $60.50 → {60.50 / 0.001}: {'OK' if (60.50 / 0.001) == round(60.50/0.001) else 'MISALIGNED'}")
print(f"EDEN tick 0.00001. SL $0.0950 → {0.0950 / 0.00001}: {'OK' if abs(round(0.0950/0.00001) - 0.0950/0.00001) < 0.01 else 'MISALIGNED'}")
print(f"EDEN TP $0.1020 → {0.1020 / 0.00001}: {'OK' if abs(round(0.1020/0.00001) - 0.1020/0.00001) < 0.01 else 'MISALIGNED'}")
