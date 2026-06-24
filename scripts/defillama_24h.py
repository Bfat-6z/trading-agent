import json, os
tmp = os.environ['TEMP']
chains = json.load(open(tmp+'\\chains.json', encoding='utf-8'))
prots = json.load(open(tmp+'\\protocols.json', encoding='utf-8'))
stables = json.load(open(tmp+'\\stables.json', encoding='utf-8'))

from collections import defaultdict
chain_tvl = defaultdict(float); chain_prev = defaultdict(float)
for p in prots:
    chain = p.get('chain') or ''
    tvl = p.get('tvl') or 0
    prev = p.get('tvlPrevDay') or 0
    if tvl and prev:
        chain_tvl[chain] += tvl
        chain_prev[chain] += prev

rows = []
for c in chain_tvl:
    if chain_prev[c] > 1e8:
        chg = (chain_tvl[c]-chain_prev[c])/chain_prev[c]*100
        rows.append((c, chain_tvl[c], chain_tvl[c]-chain_prev[c], chg))

print('=== TOP 10 CHAINS BY $ GAIN 24h ===')
for r in sorted(rows, key=lambda x:x[2], reverse=True)[:10]:
    print('{0:18} tvl=${1:.2f}B  delta=${2:+.1f}M ({3:+.2f}%)'.format(r[0], r[1]/1e9, r[2]/1e6, r[3]))

print()
print('=== TOP 10 CHAINS BY % GAIN 24h (tvl>200M) ===')
big = [r for r in rows if r[1] > 200e6]
for r in sorted(big, key=lambda x:x[3], reverse=True)[:10]:
    print('{0:18} tvl=${1:.2f}B  delta=${2:+.1f}M ({3:+.2f}%)'.format(r[0], r[1]/1e9, r[2]/1e6, r[3]))

print()
print('=== TOP 10 PROTOCOLS BY $ GAIN 24h ===')
prot_rows = []
for p in prots:
    tvl = p.get('tvl') or 0
    prev = p.get('tvlPrevDay') or 0
    if tvl > 50e6 and prev > 0:
        prot_rows.append((p.get('name'), p.get('category'), p.get('chain'), tvl, tvl-prev, (tvl-prev)/prev*100))
for r in sorted(prot_rows, key=lambda x:x[4], reverse=True)[:10]:
    print('{0:22} {1:14} {2:12} tvl=${3:.0f}M d=${4:+.1f}M ({5:+.2f}%)'.format(r[0], r[1], r[2], r[3]/1e6, r[4]/1e6, r[5]))

print()
print('=== TOP 10 PROTOCOLS BY % GAIN 24h (tvl>100M) ===')
big_p = [r for r in prot_rows if r[3] > 100e6]
for r in sorted(big_p, key=lambda x:x[5], reverse=True)[:10]:
    print('{0:22} {1:14} {2:12} tvl=${3:.0f}M d=${4:+.1f}M ({5:+.2f}%)'.format(r[0], r[1], r[2], r[3]/1e6, r[4]/1e6, r[5]))

print()
print('=== STABLECOIN 24h MCAP CHANGE ===')
peggedAssets = stables.get('peggedAssets', [])
srows = []
for s in peggedAssets:
    circ = s.get('circulating', {}).get('peggedUSD', 0) or 0
    prev = s.get('circulatingPrevDay', {}).get('peggedUSD', 0) or 0
    if prev > 0:
        srows.append((s.get('symbol'), s.get('name'), circ, circ-prev, (circ-prev)/prev*100))
for r in sorted(srows, key=lambda x:abs(x[3]), reverse=True)[:10]:
    print('{0:8} {1:28} mcap=${2:.2f}B delta=${3:+.1f}M ({4:+.3f}%)'.format(r[0] or '', r[1] or '', r[2]/1e9, r[3]/1e6, r[4]))

# Chain-level stablecoin migration
print()
print('=== STABLECOIN MIGRATION: PER-CHAIN 24h CHANGE ===')
chain_now = defaultdict(float); chain_prv = defaultdict(float)
for s in peggedAssets:
    chains_data = s.get('chainCirculating', {})
    for ch, vals in chains_data.items():
        cur = (vals.get('current') or {}).get('peggedUSD') or 0
        prv = (vals.get('circulatingPrevDay') or {}).get('peggedUSD') or 0
        chain_now[ch] += cur
        chain_prv[ch] += prv
chain_rows = []
for ch in chain_now:
    if chain_prv[ch] > 5e7:
        chain_rows.append((ch, chain_now[ch], chain_now[ch]-chain_prv[ch], (chain_now[ch]-chain_prv[ch])/chain_prv[ch]*100))
for r in sorted(chain_rows, key=lambda x:x[2], reverse=True)[:8]:
    print('GAIN  {0:20} mcap=${1:.2f}B delta=${2:+.1f}M ({3:+.3f}%)'.format(r[0], r[1]/1e9, r[2]/1e6, r[3]))
for r in sorted(chain_rows, key=lambda x:x[2])[:5]:
    print('LOSS  {0:20} mcap=${1:.2f}B delta=${2:+.1f}M ({3:+.3f}%)'.format(r[0], r[1]/1e9, r[2]/1e6, r[3]))
