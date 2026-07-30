# Run artifacts

Every Codex run exports readable audit material into this directory:

```text
<run_id>/
├── reasoning/
│   └── day-001__farm_a__turn-0001.md
└── final_outputs/
    └── day-001__farm_a__turn-0001.json
```

`reasoning` contains only public reasoning summaries exposed by the Codex
session. `final_outputs` contains the original structured response with
formatting applied for readability. Encrypted reasoning is never copied or
decrypted.

The Codex session-retention policy does not delete these exported artifacts.
Removing long-term audit results always requires an explicit maintainer action.
