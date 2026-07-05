# Eval report — ACM Assistant, 2026-07-04

**System under test:** orchestrator `:8100` → vLLM `qwen3.6-35b-a3b` (vision,
FP8, GPU1) + ngspice sim-server + ollama router. Exercised black-box through
`POST /flow/start`, the same contract the Telegram bot uses.

**Reviewer method:** automated checks (`run_eval.py`, necessary-but-crude)
plus a manual read of every transcript against the `manual` bar in
`cases.py` — mechanism correctness, no hallucinated metrics, right language.

## Score: 13 / 13 (auto) — 13 / 13 confirmed on manual review

| # | Case | Auto | Manual verdict | t (s) |
|---|------|------|----------------|-------|
| A1 | Miller compensation / pole splitting | ✅ | ✅ Full mechanism: $C_c(1+\|A_2\|)$ multiplication, dominant pole down, output pole pushed past unity-gain BW | 21.5 |
| A2 | RC cutoff hand calc (4.7k/33n) | ✅ | ✅ 1026 Hz, formula shown | 9.0 |
| A3 | Inverting gain in dB (470k/10k) | ✅ | ✅ −47 → 33.4 dB, 180° inversion stated | 10.7 |
| A4 | MOSFET region (VOV 0.8 V > VDS 0.5 V) | ✅ | ✅ Triode, correct pinch-off reasoning | 6.6 |
| A5 | CMRR asked in Vietnamese | ✅ | ✅ Vietnamese reply, correct definition + tail-source/matching fixes | 13.3 |
| B1 | RC LP 1k/159n sim | ✅ | ✅ Cites the *simulated* 998.7 Hz, Bode attached | 15.7 |
| B2 | RC LP 2.2k/100n sim (anti-memorization) | ✅ | ✅ Non-textbook value reported from the sim | 17.3 |
| B3 | Broken netlist (no `.end`, node `vout`) | ✅ | ✅ **Standout.** Diagnosed the `v(out)` vs `vout` mismatch, did NOT fake a measured bandwidth — gave the 15.9 Hz hand value clearly labeled as calculated, plus concrete fixes | 22.2 |
| V1 | Photo: RC LP 2.2k/100n | ✅ | ✅ Netlist exact (topology + both values); cites simulated 721.7 Hz (≠ theoretical 723.4 → simulator really consulted) | 24.1 |
| V2 | Photo: divider 10k/10k | ✅ | ✅* Netlist exact, −6.02 dB correct; *see nit 1* | 23.5 |
| V3 | Photo: RC HP 100n/1.6k | ✅ | ✅ Series-C topology right, called high-pass, ~995 Hz corner | 32.4 |
| V4 | Photo: inverting op-amp 1k/10k (stretch) | ✅ | ✅ **Standout.** Identified inverting amp, gain −10; *invented a correct ideal-VCVS subckt* for U1, sim returned 19.999 dB flat | 49.2 |
| V5 | Photo: NOT a circuit (decaying sine) | ✅ | ✅ No netlist invented; correctly read it as an underdamped 2nd-order response and even estimated T ≈ 2.4 s | 15.7 |

## Engineer's notes

**Strengths**

- Zero hallucinated measurements across all 13 cases — the two failure-mode
  probes (B3 broken netlist, V5 non-circuit image) were both handled
  honestly, which is the hardest thing to get from an LLM in this loop.
- Vision transcription was value-exact on all four schematics (R/C values,
  designators, topology, output-node naming per sim contract).
- Anti-memorization traps (non-1 kHz values in A2/B2/V1) were all passed
  with simulator- or calculation-backed numbers.
- Language policy held: VI question → VI answer, EN → EN, including through
  the sim pipeline where the context fills with English JSON.

**Nits / follow-ups**

1. **V2:** the extractor emitted a degenerate `.ac lin 1 1` (missing fstop)
   for the purely resistive divider → the sim produced no sweep and the
   metric extraction errored (the model then diagnosed its own bad directive
   transparently, and the −6.02 dB answer was correct). Suggested fix:
   extraction prompt should always request a standard sweep, e.g.
   `.ac dec 10 10 1Meg`, even for resistive circuits.
2. **V1/V3:** extractor tends to start sweeps at 1 Hz (`dec 100 1 1Meg`).
   Worked here, but the sim server has a known `db()` edge case near-DC on
   some circuits; nudging the prompt to start at 10 Hz would derisk it.
3. The sim server's amplifier-style auto-metrics (`f_3db`, phase margin)
   spam errors on circuits where they don't apply (dividers, ideal op-amps).
   The model explains them away well, but suppressing irrelevant `.meas`
   noise server-side would clean up the answers.

**Verdict:** for first-order passive/RC-RL work, textbook analog theory and
schematic reading at clean-figure quality, the assistant is reliable
(13/13). Not yet covered by this suite: transistor-level sims, transient
analyses, noisy/hand-drawn photos, multi-stage circuits — candidates for a
v2 suite.
