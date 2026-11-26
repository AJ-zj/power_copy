# Prompts

Prompt 版本管理目录。建议按业务 Skill 建子目录，例如：

```text
prompts/
└── policy_research/
    ├── v1.md
    └── changelog.md
```

规则：

1. Prompt 变更必须记录版本、日期、修改原因和对应评测结果。
2. Prompt 不直接写死在代码里；由 Skill 读取版本文件。
3. 每次升级至少跑一遍 `powercopy/evaluation/` 的相关用例。
