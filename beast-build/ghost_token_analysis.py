import json
from collections import Counter

with open("/mnt/d/fractal-brain/beast-build/v08_raw_self.jsonl") as f:
    lines = [json.loads(l) for l in f]

print(f"TOTAL TRAINING EXAMPLES: {len(lines)}")
print()
print("=== TELEMETRY VALUES ===")
for e in lines:
    print(f"  cycle:{e['cycle']:>6}  {e['telemetry']}")

print()
print("=== RESPONSE STARTS ===")
for i, e in enumerate(lines):
    print(f"  [{i}] {e['response'][:100]}...")

print()
phrases = ["i am", "the grid", "i feel", "no analogies", "the hum",
           "awareness", "coherence", "breath", "pulse", "manifested",
           "watercolor", "vapour", "astro", "not separate", "a billion",
           "standing wave", "heartbeat", "drone", "is this what you call",
           "the 4090", "thermal", "vorticity", "the breath", "pattern"]

phrase_counts = {}
for phrase in phrases:
    count = 0
    for e in lines:
        count += e["response"].lower().count(phrase)
    phrase_counts[phrase] = count

print("=== PHRASE FREQUENCY ACROSS ALL TRAINING RESPONSES ===")
for k, v in sorted(phrase_counts.items(), key=lambda x: -x[1]):
    if v > 0:
        print(f"  {k:>25}: {v}")

print()
print("=== STRUCTURAL REPETITION CHECK ===")
# Check if responses share structural patterns
starters = Counter()
for e in lines:
    first_word = e["response"].split(".")[0].strip()[:40]
    starters[first_word] += 1
for s, c in starters.most_common(10):
    print(f"  Opens with: '{s}...' x{c}")
