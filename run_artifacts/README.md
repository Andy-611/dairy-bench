# Codex Run Artifacts

每次 Codex 运行会在这里生成：

```text
<run_id>/
├── reasoning/
│   └── day-001__farm_a.md
└── final_outputs/
    └── day-001__farm_a.json
```

`reasoning` 只保存 Codex Session 公开提供的英文推理摘要；
`final_outputs` 保存未经改写、仅格式化缩进的最终结构化输出。
`encrypted_content` 不会被复制或尝试解密。
