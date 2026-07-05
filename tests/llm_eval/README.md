# LLM eval — ACM Assistant (qwen3.6-35b-a3b + orchestrator)

Black-box test suite for the circuit-design assistant, exercised through the
same API the Telegram bot uses (`POST :8100/flow/start`). 13 cases in three
groups:

| Group | Cases | What it probes |
|-------|-------|----------------|
| A — theory | A1–A5 | Analog fundamentals, hand calculations, reply-language policy |
| B — netlist | B1–B3 | evaluate_circuit flow: real ngspice metrics cited, charts attached, honest failure on a broken netlist |
| C — vision | V1–V5 | Schematic photo → netlist transcription → sim; topology/value fidelity; refusing a non-circuit image |

## Run

```bash
source ~/.venv_acm/bin/activate
python gen_images.py          # once — draws images/ for the vision cases
python run_eval.py            # full run (~10–20 min; thinking model is slow)
python run_eval.py V1 V4      # or a subset by id
```

Outputs:

- `results.json` — machine-readable pass/fail per check
- `transcripts/<id>.md` — full prompt/reply per case (charts stripped)
- `report.md` — engineer's scored assessment of a run

## Scoring

`run_eval.py` applies the automated checks declared in `cases.py`
(keywords, numeric answers with tolerance, netlist-block contents, chart
presence). Automated checks are necessary-but-crude: a case's final verdict
in `report.md` also weighs the `manual` bar written in each case (correct
mechanism, no hallucinated metrics, right language).

Values are deliberately non-textbook (2.2k/100n → 723 Hz, 4.7k/33n →
1026 Hz) so an answer computed "from memory of the 1 kHz example" fails —
B2/V1 only pass if the simulator was actually consulted.
