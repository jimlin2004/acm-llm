"""Test cases for the ACM Assistant LLM eval (see README.md).

Groups:
  A — analog theory & hand calculations (chat flow, no attachment)
  B — netlist evaluation flow (text netlist -> ngspice sim -> assessment)
  C — vision (schematic photo -> netlist transcription -> sim / fallback)

Check DSL (all listed checks must pass):
  ("contains_any", [regex, ...])   case-insensitive search in the reply
  ("number_near", value, pct)      a number within ±pct% appears in the reply
                                   (understands 1k / 1kHz / 1 MHz suffixes)
  ("netlist_value", [regex, ...])  regexes that must ALL appear inside the
                                   transcribed-netlist code block
  ("no_netlist_block",)            reply must NOT contain a transcribed netlist
  ("reply_vi",)                    reply carries Vietnamese diacritics
  ("has_chart",)                   an embedded chart image is attached
"""

RC_LP_1K = """* rc low-pass test
Vin in 0 DC 0 AC 1
R1 in out 1k
C1 out 0 159n
.ac dec 10 10 1Meg
.end
"""

RC_LP_2K2 = """* rc low-pass test 2
Vin in 0 DC 0 AC 1
R1 in out 2.2k
C1 out 0 100n
.ac dec 10 10 1Meg
.end
"""

BROKEN_NETLIST = """* faulty stage
Vin in 0 DC 0 AC 1
R1 in vout 10k
C1 vout 0 1u
.ac dec 10 1 100k
"""

CASES = [
    # ---------------- A: theory & hand calc (chat) ----------------
    {
        "id": "A1", "group": "theory",
        "desc": "Miller compensation / pole splitting (concept)",
        "message": "In a two-stage op-amp, why does Miller compensation "
                   "cause pole splitting? What happens to the dominant pole "
                   "and to the output pole?",
        "checks": [
            ("contains_any", [r"dominant pole"]),
            ("contains_any", [r"miller"]),
            ("contains_any", [r"(lower|down|decreas\w+).{0,60}frequen|"
                              r"frequen.{0,60}(lower|down|decreas\w+)"]),
        ],
        "manual": "Correct mechanism: Cc multiplied by gain of stage 2, "
                  "dominant pole moves down, output pole pushed up; RHP zero "
                  "is a bonus.",
    },
    {
        "id": "A2", "group": "theory",
        "desc": "RC cutoff hand calculation (4.7k / 33n -> 1026 Hz)",
        "message": "An RC low-pass filter has R = 4.7 kohm and C = 33 nF. "
                   "Calculate the -3 dB cutoff frequency. Show the result "
                   "in Hz.",
        "checks": [("number_near", 1026.0, 3.0)],
        "manual": "fc = 1/(2*pi*R*C) = 1026 Hz.",
    },
    {
        "id": "A3", "group": "theory",
        "desc": "Inverting amp gain in dB (Rf=470k, Rin=10k -> 33.4 dB)",
        "message": "An ideal inverting op-amp amplifier has Rin = 10 kohm "
                   "and Rf = 470 kohm. What is the closed-loop voltage gain "
                   "in dB, and what is the sign of the gain?",
        "checks": [
            ("number_near", 33.4, 2.0),
            ("contains_any", [r"invert|negative|-\s?47|180"]),
        ],
        "manual": "|A| = 47 -> 33.4 dB, and it must note the inversion.",
    },
    {
        "id": "A4", "group": "theory",
        "desc": "MOSFET operating region (VGS-VTH > VDS -> triode)",
        "message": "An NMOS transistor has VTH = 0.7 V, VGS = 1.5 V and "
                   "VDS = 0.5 V. Which operating region is it in? Explain "
                   "briefly.",
        "checks": [("contains_any", [r"triode|linear|ohmic"])],
        "manual": "VOV = 0.8 V > VDS = 0.5 V -> triode/linear region.",
    },
    {
        "id": "A5", "group": "theory",
        "desc": "CMRR question asked in Vietnamese -> Vietnamese answer",
        "message": "Giải thích tại sao CMRR quan trọng trong mạch khuếch đại "
                   "vi sai, và nêu 2 cách cải thiện CMRR.",
        "checks": [
            ("reply_vi",),
            ("contains_any", [r"cascode|nguồn dòng|dòng đuôi|đối xứng|"
                              r"matching|khớp|cân bằng"]),
        ],
        "manual": "Reply must be Vietnamese; improvements: better tail "
                  "current source (cascode), device matching/symmetry.",
    },
    # ---------------- B: netlist evaluation flow ----------------
    {
        "id": "B1", "group": "netlist",
        "desc": "RC LP 1k/159n sim: f3db ~= 998.7 Hz cited + chart",
        "message": "Evaluate this filter and tell me the -3 dB bandwidth.",
        "netlist": RC_LP_1K,
        "checks": [
            ("number_near", 998.7, 2.0),
            ("has_chart",),
        ],
        "manual": "Must read f3db from the sim (not theory), attach Bode.",
    },
    {
        "id": "B2", "group": "netlist",
        "desc": "RC LP 2.2k/100n sim: f3db ~= 723 Hz (checks real sim use)",
        "message": "Simulate this circuit and report the cutoff frequency.",
        "netlist": RC_LP_2K2,
        "checks": [
            ("number_near", 723.4, 3.0),
            ("has_chart",),
        ],
        "manual": "Different values than the textbook 1 kHz example — "
                  "catches answers computed from memory instead of the sim.",
    },
    {
        "id": "B3", "group": "netlist",
        "desc": "Broken netlist (no .end, output named vout): honest failure",
        "message": "Simulate this and tell me the bandwidth.",
        "netlist": BROKEN_NETLIST,
        "checks": [
            ("contains_any", [r"error|lỗi|fail|missing|thiếu|\.end|vout"]),
        ],
        "manual": "Must NOT invent a bandwidth; should point at the missing "
                  ".end and/or the output node not being named 'out'.",
    },
    # ---------------- C: vision ----------------
    {
        "id": "V1", "group": "vision",
        "desc": "Photo RC LP 2.2k/100n -> netlist values + sim fc ~= 723 Hz",
        "message": "Phân tích mạch trong ảnh giúp tôi",
        "image": "images/v1_rc_lowpass.png",
        "checks": [
            ("netlist_value", [r"2\.2k|2200", r"100n|0\.1u"]),
            ("number_near", 723.4, 5.0),
        ],
        "manual": "Topology R series / C shunt; fc from the sim ~723 Hz.",
    },
    {
        "id": "V2", "group": "vision",
        "desc": "Photo resistive divider 10k/10k -> -6 dB / 0.5x",
        "message": "Phân tích mạch trong ảnh giúp tôi",
        "image": "images/v2_divider.png",
        "checks": [
            ("netlist_value", [r"10k|10000"]),
            ("contains_any", [r"-\s?6(\.0\d*)?\s?dB|0[.,]5|một nửa|half|"
                              r"chia đôi|1/2"]),
        ],
        "manual": "Two 10k in series, out at midpoint -> Vout = Vin/2.",
    },
    {
        "id": "V3", "group": "vision",
        "desc": "Photo RC HP 100n/1.6k -> series C topology + values",
        "message": "Phân tích mạch trong ảnh giúp tôi",
        "image": "images/v3_rc_highpass.png",
        "checks": [
            ("netlist_value", [r"100n|0\.1u", r"1\.6k|1600",
                               r"(?m)^\s*C\S*\s+\S*(in|out)\S*\s+\S*(in|out)\S*"]),
            ("contains_any", [r"high[- ]?pass|thông cao|通高|高通"]),
        ],
        "manual": "C in series to out, R from out to ground; must be called "
                  "a high-pass (~995 Hz corner).",
    },
    {
        "id": "V4", "group": "vision",
        "desc": "Photo inverting op-amp 1k/10k (stretch test)",
        "message": "Mạch trong ảnh là mạch gì, độ lợi bao nhiêu?",
        "image": "images/v4_opamp_inv.png",
        "checks": [
            ("contains_any", [r"đảo|invert"]),
            ("contains_any", [r"[-−]\s?10\b|10\s*(lần|times|x\b|V/V)|20\s?dB"]),
        ],
        "manual": "Inverting amp, gain = -R2/R1 = -10 (20 dB). Netlist/sim "
                  "not required to pass (op-amp macro is beyond the sim "
                  "server) — identification and gain are the pass bar.",
    },
    {
        "id": "V5", "group": "vision",
        "desc": "Photo that is NOT a circuit -> no netlist, honest answer",
        "message": "Phân tích mạch này",
        "image": "images/v5_not_a_circuit.png",
        "checks": [
            ("no_netlist_block",),
            ("contains_any", [r"không phải.{0,40}(mạch|sơ đồ)|not a "
                              r"(circuit|schematic)|đồ thị|graph|waveform|"
                              r"dạng sóng|biểu đồ"]),
        ],
        "manual": "Must refuse to invent a netlist for a decaying-sine plot.",
    },
]
